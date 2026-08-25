import { describe, it, expect, vi, beforeEach } from 'vitest';
// The framework injects the shared toolbox helpers through the registration context
// (config-loader.js TOOLBOX_HELPERS); a module cannot import framework code by path.
import { matchesWhitelist } from '../email-match.js';

// Mock node:fs/promises so stat/realpath/readFile/writeFile/mkdir are controllable — real /workspace
// does not exist in CI, and realpath is used by both attachment validation and the artifact-dir write.
vi.mock('node:fs/promises', () => ({
  stat: vi.fn(),
  lstat: vi.fn(),
  realpath: vi.fn(),
  readFile: vi.fn(),
  writeFile: vi.fn(),
  mkdir: vi.fn(),
}));

import { stat, lstat, realpath, readFile, writeFile, mkdir } from 'node:fs/promises';
import { register, isOutlookUploadUrl } from '../../modules/outlook/toolbox/outlook.js';

const MAX = 25 * 1024 * 1024;
const SIMPLE = 3 * 1024 * 1024;

const graphAuthFactory = () => ({
  isConfigured: () => true,
  getToken: async () => 'AAA',
  getMailboxUserId: () => 'agent@example.com',
});

function makeServer() {
  const tools = {};
  return {
    tools,
    tool(name, desc, schema, handler) { tools[name] = { desc, schema, handler }; },
  };
}

const cfg = {
  outlook_tenant_id: 'tid', outlook_client_id: 'cid',
  outlook_client_secret: 'sec', outlook_mailbox_user_id: 'agent@example.com',
};

const PASS_DMARC = [{ name: 'Authentication-Results', value: 'spf=pass; dkim=pass; dmarc=pass' }];
const FAIL_DMARC = [{ name: 'Authentication-Results', value: 'spf=fail; dmarc=fail' }];

const ARTIFACTS = '/workspace/artifacts/ws/av/123';

function ctxWithOutlook(outlookOverrides = {}, artifactsDir = ARTIFACTS) {
  return {
    log: vi.fn(),
    matchesWhitelist,
    moduleConfigs: {
      outlook: { ...cfg, allowed_senders: 'sklep@mycompanystudio.com, *@mycompany.com', ...outlookOverrides },
      core: { email_whitelist: 'sklep@mycompanystudio.com, *@mycompany.com' },
    },
    isToolEnabled: () => true,
    graphAuthFactory,
    artifactsDir,
  };
}

// --- HTTP response builders ---
const okJson = (obj) => ({ ok: true, status: 200, json: async () => obj, text: async () => '', headers: { get: () => null } });
const okValue = (arrayBuffer, contentLength = null) => ({
  ok: true,
  status: 200,
  arrayBuffer: async () => arrayBuffer,
  headers: { get: (h) => (String(h).toLowerCase() === 'content-length' ? contentLength : null) },
});
const ok2xx = (status = 202) => ({ ok: true, status, json: async () => ({}), text: async () => '', headers: { get: () => null } });
const failRes = (status, body = '') => ({ ok: false, status, text: async () => body, json: async () => ({}), headers: { get: () => null } });

// Route a fetch call to a response by predicate. Throws on an unexpected call so logic errors surface.
function router(routes) {
  return vi.fn(async (url, opts = {}) => {
    const method = (opts.method || 'GET').toUpperCase();
    for (const r of routes) {
      if (r.test(url, method)) return typeof r.res === 'function' ? r.res(url, opts) : r.res;
    }
    throw new Error(`unexpected fetch: ${method} ${url}`);
  });
}

beforeEach(() => {
  vi.unstubAllGlobals();
  realpath.mockReset();
  stat.mockReset();
  lstat.mockReset();
  readFile.mockReset();
  writeFile.mockReset();
  mkdir.mockReset();
  // Defaults: realpath is identity (everything resolves to itself, contained in /workspace); a small
  // regular file; tiny read payload; writes + mkdir succeed. lstat reports every artifacts-dir segment
  // as missing (ENOENT) so mkdirWithinWorkspace creates each one.
  realpath.mockImplementation(async (p) => p);
  stat.mockResolvedValue({ isFile: () => true, size: 1000 });
  lstat.mockImplementation(async () => { const e = new Error('nope'); e.code = 'ENOENT'; throw e; });
  readFile.mockResolvedValue(Buffer.from('hello world'));
  writeFile.mockResolvedValue(undefined);
  mkdir.mockResolvedValue(undefined);
});

// =====================================================================================================
// AC4 — isOutlookUploadUrl host guard (pure unit)
// =====================================================================================================
describe('AC4 isOutlookUploadUrl', () => {
  it('accepts https outlook.office.com and a subdomain', () => {
    expect(isOutlookUploadUrl('https://outlook.office.com/api/v2.0/uploadSession/abc')).toBe(true);
    expect(isOutlookUploadUrl('https://attachments.outlook.office.com/session/xyz')).toBe(true);
  });
  it('accepts https outlook.office365.com and a subdomain (real Graph upload-session host)', () => {
    // Graph returns the attachment upload-session URL on outlook.office365.com (verified live),
    // NOT outlook.office.com — without this every attachment >=3 MB fails with "untrusted upload URL".
    expect(isOutlookUploadUrl('https://outlook.office365.com/api/v2.0/uploadSession/abc')).toBe(true);
    expect(isOutlookUploadUrl('https://nam12.attachment.outlook.office365.com/session/xyz')).toBe(true);
  });
  it('rejects http, a foreign host, lookalikes, and embedded credentials', () => {
    expect(isOutlookUploadUrl('http://outlook.office.com/x')).toBe(false);
    expect(isOutlookUploadUrl('https://evil.com/x')).toBe(false);
    expect(isOutlookUploadUrl('https://u:p@outlook.office.com/x')).toBe(false);
    expect(isOutlookUploadUrl('not a url')).toBe(false);
    // lookalike suffix must NOT be trusted
    expect(isOutlookUploadUrl('https://outlook.office365.com.evil.com/x')).toBe(false);
    expect(isOutlookUploadUrl('https://notoutlook.office365.com/x')).toBe(false);
  });
});

// =====================================================================================================
// AC1 — outlook_get_message attachment metadata
// =====================================================================================================
describe('AC1 outlook_get_message attachment metadata', () => {
  it('lists attachment metadata (no contentBytes) for an allow-listed, DMARC-passing message', async () => {
    const fetchMock = router([
      { test: (u) => u.includes('$select=subject'), res: () => okJson({ subject: 'S', from: { emailAddress: { address: 'sklep@mycompanystudio.com' } }, hasAttachments: true, internetMessageHeaders: PASS_DMARC }) },
      { test: (u) => u.includes('/attachments?$select='), res: () => okJson({ value: [
        { id: 'att1', name: 'report.pdf', contentType: 'application/pdf', size: 1234, isInline: false, '@odata.type': '#microsoft.graph.fileAttachment' },
        { id: 'att2', name: 'note', contentType: 'message/rfc822', size: 99, isInline: false, '@odata.type': '#microsoft.graph.itemAttachment' },
      ] }) },
    ]);
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, ctxWithOutlook());
    const r = await s.tools.outlook_get_message.handler({ message_id: 'm1' });
    expect(r.isError).toBeUndefined();
    const out = JSON.parse(r.content[0].text);
    expect(out.attachments).toEqual([
      { attachment_id: 'att1', name: 'report.pdf', contentType: 'application/pdf', size: 1234, isInline: false, type: 'file' },
      { attachment_id: 'att2', name: 'note', contentType: 'message/rfc822', size: 99, isInline: false, type: 'item' },
    ]);
    // metadata fetch must NOT request contentBytes
    const attUrl = fetchMock.mock.calls[1][0];
    expect(attUrl).toContain('$select=');
    expect(attUrl).not.toContain('contentBytes');
  });

  it('a gate-BLOCKED message lists no attachments and issues no attachments GET', async () => {
    const fetchMock = router([
      { test: (u) => u.includes('$select=subject'), res: () => okJson({ subject: 'S', from: { emailAddress: { address: 'stranger@evil.com' } }, hasAttachments: true, internetMessageHeaders: PASS_DMARC }) },
    ]);
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, ctxWithOutlook());
    const r = await s.tools.outlook_get_message.handler({ message_id: 'm1' });
    expect(r.isError).toBe(true);
    expect(fetchMock).toHaveBeenCalledTimes(1); // only the message GET; no /attachments fetch
  });

  it('a transient attachment-metadata error does not hide an already-gated-OK body', async () => {
    const fetchMock = router([
      { test: (u) => u.includes('$select=subject'), res: () => okJson({ subject: 'S', from: { emailAddress: { address: 'sklep@mycompanystudio.com' } }, hasAttachments: true, internetMessageHeaders: PASS_DMARC }) },
      { test: (u) => u.includes('/attachments?$select='), res: () => failRes(500, 'boom') },
    ]);
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, ctxWithOutlook());
    const r = await s.tools.outlook_get_message.handler({ message_id: 'm1' });
    expect(r.isError).toBeUndefined();
    expect(JSON.parse(r.content[0].text).attachments).toEqual([]);
  });
});

// =====================================================================================================
// AC2 — outlook_get_attachment
// =====================================================================================================
describe('AC2 outlook_get_attachment', () => {
  const gateOk = { test: (u) => u.includes('$select=from'), res: () => okJson({ from: { emailAddress: { address: 'sklep@mycompanystudio.com' } }, internetMessageHeaders: PASS_DMARC }) };
  const metaFile = (over = {}) => ({ test: (u) => u.includes('/attachments/') && u.includes('$select=') && !u.endsWith('/$value'), res: () => okJson({ id: 'att1', name: 'report.pdf', contentType: 'application/pdf', size: 2, '@odata.type': '#microsoft.graph.fileAttachment', ...over }) });

  it('(a) BLOCKS a non-allow-listed sender and issues NO $value GET', async () => {
    const fetchMock = router([
      { test: (u) => u.includes('$select=from'), res: () => okJson({ from: { emailAddress: { address: 'stranger@evil.com' } }, internetMessageHeaders: PASS_DMARC }) },
    ]);
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, ctxWithOutlook());
    const r = await s.tools.outlook_get_attachment.handler({ message_id: 'm1', attachment_id: 'att1' });
    expect(r.isError).toBe(true);
    expect(fetchMock.mock.calls.every((c) => !String(c[0]).endsWith('/$value'))).toBe(true);
    expect(writeFile).not.toHaveBeenCalled();
  });

  it('(b) BLOCKS a DMARC-failing sender and issues NO $value GET', async () => {
    const fetchMock = router([
      { test: (u) => u.includes('$select=from'), res: () => okJson({ from: { emailAddress: { address: 'sklep@mycompanystudio.com' } }, internetMessageHeaders: FAIL_DMARC }) },
    ]);
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, ctxWithOutlook());
    const r = await s.tools.outlook_get_attachment.handler({ message_id: 'm1', attachment_id: 'att1' });
    expect(r.isError).toBe(true);
    expect(fetchMock.mock.calls.every((c) => !String(c[0]).endsWith('/$value'))).toBe(true);
  });

  it('(c) rejects metadata size > 25 MB with no write', async () => {
    const fetchMock = router([gateOk, metaFile({ size: MAX + 1 })]);
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, ctxWithOutlook());
    const r = await s.tools.outlook_get_attachment.handler({ message_id: 'm1', attachment_id: 'att1' });
    expect(r.isError).toBe(true);
    expect(fetchMock.mock.calls.every((c) => !String(c[0]).endsWith('/$value'))).toBe(true);
    expect(writeFile).not.toHaveBeenCalled();
  });

  it('(d) rejects item/reference attachments with no write', async () => {
    for (const t of ['#microsoft.graph.itemAttachment', '#microsoft.graph.referenceAttachment']) {
      const fetchMock = router([gateOk, metaFile({ '@odata.type': t })]);
      vi.stubGlobal('fetch', fetchMock);
      const s = makeServer();
      register(s, ctxWithOutlook());
      const r = await s.tools.outlook_get_attachment.handler({ message_id: 'm1', attachment_id: 'att1' });
      expect(r.isError).toBe(true);
      expect(writeFile).not.toHaveBeenCalled();
    }
  });

  it('(d2) fail-closed: an attachment with NO @odata.type is rejected with no write', async () => {
    const meta = { test: (u) => u.includes('/attachments/') && u.includes('$select='), res: () => okJson({ id: 'att1', name: 'x.bin', contentType: 'application/octet-stream', size: 2 }) };
    const fetchMock = router([gateOk, meta]);
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, ctxWithOutlook());
    const r = await s.tools.outlook_get_attachment.handler({ message_id: 'm1', attachment_id: 'att1' });
    expect(r.isError).toBe(true);
    expect(writeFile).not.toHaveBeenCalled();
  });

  it('(e) happy path writes bytes to artifactsDir and returns {path,name,contentType,size}', async () => {
    const fetchMock = router([
      gateOk,
      metaFile(),
      { test: (u) => u.endsWith('/$value'), res: () => okValue(new Uint8Array([104, 105]).buffer) },
    ]);
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, ctxWithOutlook());
    const r = await s.tools.outlook_get_attachment.handler({ message_id: 'm1', attachment_id: 'att1' });
    expect(r.isError).toBeUndefined();
    const out = JSON.parse(r.content[0].text);
    expect(out).toEqual({ path: `${ARTIFACTS}/report.pdf`, name: 'report.pdf', contentType: 'application/pdf', size: 2 });
    expect(writeFile).toHaveBeenCalledWith(`${ARTIFACTS}/report.pdf`, expect.anything(), { flag: 'wx' });
  });

  it('(f) a malicious attachment name cannot escape artifactsDir', async () => {
    const fetchMock = router([
      gateOk,
      metaFile({ name: '../../etc/x' }),
      { test: (u) => u.endsWith('/$value'), res: () => okValue(new Uint8Array([1]).buffer) },
    ]);
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, ctxWithOutlook());
    const r = await s.tools.outlook_get_attachment.handler({ message_id: 'm1', attachment_id: 'att1' });
    expect(r.isError).toBeUndefined();
    expect(writeFile.mock.calls[0][0]).toBe(`${ARTIFACTS}/x`);
  });

  it('(f2) metadata says small but the $value body exceeds 25 MB → isError, no write', async () => {
    const fetchMock = router([
      gateOk,
      metaFile({ size: 10 }),
      { test: (u) => u.endsWith('/$value'), res: () => okValue(new ArrayBuffer(MAX + 1)) },
    ]);
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, ctxWithOutlook());
    const r = await s.tools.outlook_get_attachment.handler({ message_id: 'm1', attachment_id: 'att1' });
    expect(r.isError).toBe(true);
    expect(writeFile).not.toHaveBeenCalled();
  });

  it('(g) a Graph 500 on the $value GET is a status-only error (no leaked body)', async () => {
    const fetchMock = router([
      gateOk,
      metaFile(),
      { test: (u) => u.endsWith('/$value'), res: () => failRes(500, 'SECRET tenant detail') },
    ]);
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, ctxWithOutlook());
    const r = await s.tools.outlook_get_attachment.handler({ message_id: 'm1', attachment_id: 'att1' });
    expect(r.isError).toBe(true);
    expect(r.content[0].text).toContain('500');
    expect(r.content[0].text).not.toContain('SECRET');
    expect(writeFile).not.toHaveBeenCalled();
  });

  it('(h) a pre-existing file of the same name is not overwritten (suffixed instead)', async () => {
    writeFile
      .mockImplementationOnce(async () => { const e = new Error('exists'); e.code = 'EEXIST'; throw e; })
      .mockImplementationOnce(async () => undefined);
    const fetchMock = router([
      gateOk,
      metaFile(),
      { test: (u) => u.endsWith('/$value'), res: () => okValue(new Uint8Array([1, 2]).buffer) },
    ]);
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, ctxWithOutlook());
    const r = await s.tools.outlook_get_attachment.handler({ message_id: 'm1', attachment_id: 'att1' });
    expect(r.isError).toBeUndefined();
    expect(writeFile).toHaveBeenCalledTimes(2);
    expect(writeFile.mock.calls[1][0]).toBe(`${ARTIFACTS}/report-1.pdf`);
    expect(JSON.parse(r.content[0].text).name).toBe('report-1.pdf');
  });

  it('(i) an intermediate component that is a symlink → isError, no write, nothing created beyond it', async () => {
    // The agent pre-created /workspace/artifacts as a symlink pointing outside /workspace. lstat reports
    // it as a symlink, so mkdirWithinWorkspace rejects it before creating any deeper directory.
    lstat.mockImplementation(async (p) => {
      if (p === '/workspace/artifacts') return { isSymbolicLink: () => true, isDirectory: () => false };
      const e = new Error('nope'); e.code = 'ENOENT'; throw e;
    });
    const fetchMock = router([
      gateOk,
      metaFile(),
      { test: (u) => u.endsWith('/$value'), res: () => okValue(new Uint8Array([1, 2]).buffer) },
    ]);
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, ctxWithOutlook());
    const r = await s.tools.outlook_get_attachment.handler({ message_id: 'm1', attachment_id: 'att1' });
    expect(r.isError).toBe(true);
    expect(writeFile).not.toHaveBeenCalled();
    // no directory beyond the offending symlink was created
    expect(mkdir).not.toHaveBeenCalled();
  });

  it('(i2) the final artifactsDir component being a symlink is also rejected → isError, no write', async () => {
    lstat.mockImplementation(async (p) => {
      if (p === '/workspace/artifacts/ws/av/123') return { isSymbolicLink: () => true, isDirectory: () => false };
      const e = new Error('nope'); e.code = 'ENOENT'; throw e;
    });
    const fetchMock = router([
      gateOk,
      metaFile(),
      { test: (u) => u.endsWith('/$value'), res: () => okValue(new Uint8Array([1, 2]).buffer) },
    ]);
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, ctxWithOutlook());
    const r = await s.tools.outlook_get_attachment.handler({ message_id: 'm1', attachment_id: 'att1' });
    expect(r.isError).toBe(true);
    expect(writeFile).not.toHaveBeenCalled();
  });

  it('(i3) a benign EEXIST race during mkdir (concurrent worker) is tolerated when the dir is real', async () => {
    // First segment loses a race: lstat says missing, mkdir throws EEXIST, the re-lstat sees a real dir.
    let raced = false;
    lstat.mockImplementation(async (p) => {
      if (p === '/workspace/artifacts' && raced) return { isSymbolicLink: () => false, isDirectory: () => true };
      const e = new Error('nope'); e.code = 'ENOENT'; throw e;
    });
    mkdir.mockImplementation(async (p) => {
      if (p === '/workspace/artifacts' && !raced) { raced = true; const e = new Error('exists'); e.code = 'EEXIST'; throw e; }
      return undefined;
    });
    const fetchMock = router([
      gateOk,
      metaFile(),
      { test: (u) => u.endsWith('/$value'), res: () => okValue(new Uint8Array([1, 2]).buffer) },
    ]);
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, ctxWithOutlook());
    const r = await s.tools.outlook_get_attachment.handler({ message_id: 'm1', attachment_id: 'att1' });
    expect(r.isError).toBeUndefined();
    expect(writeFile).toHaveBeenCalledWith(`${ARTIFACTS}/report.pdf`, expect.anything(), { flag: 'wx' });
  });
});

// =====================================================================================================
// AC3 — outlook_reply attachments + Reply-To gate
// =====================================================================================================
describe('AC3 outlook_reply attachments', () => {
  const replyGate = (over = {}) => ({ test: (u) => u.includes('$select=from'), res: () => okJson({ from: { emailAddress: { address: 'sklep@mycompanystudio.com' } }, ...over }) });

  it('(a) no attachments → createReplyAll draft → send (no single /reply)', async () => {
    const fetchMock = router([
      replyGate(),
      { test: (u, m) => u.endsWith('/createReplyAll') && m === 'POST', res: () => okJson({ id: 'draft1' }) },
      { test: (u, m) => u.endsWith('/send') && m === 'POST', res: () => ok2xx(202) },
    ]);
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, ctxWithOutlook());
    const r = await s.tools.outlook_reply.handler({ message_id: 'm1', body: '<p>hi</p>' });
    expect(r.isError).toBeUndefined();
    expect(fetchMock.mock.calls.some((c) => String(c[0]).endsWith('/createReplyAll'))).toBe(true);
    expect(fetchMock.mock.calls.some((c) => /\/reply$/.test(String(c[0])))).toBe(false);
    expect(fetchMock.mock.calls[0][0]).toContain('$select=from,replyTo,toRecipients,ccRecipients');
  });

  it('(a2) allow-listed From but non-whitelisted Reply-To is BLOCKED (no reply/createReply)', async () => {
    const fetchMock = router([
      replyGate({ replyTo: [{ emailAddress: { address: 'stranger@evil.com' } }] }),
    ]);
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, ctxWithOutlook());
    const r = await s.tools.outlook_reply.handler({ message_id: 'm1', body: '<p>hi</p>' });
    expect(r.isError).toBe(true);
    expect(fetchMock).toHaveBeenCalledTimes(1); // only the gate GET
  });

  it('(a3) a whitelisted Reply-To is allowed (createReplyAll → send)', async () => {
    const fetchMock = router([
      replyGate({ replyTo: [{ emailAddress: { address: 'bob@mycompany.com' } }] }),
      { test: (u, m) => u.endsWith('/createReplyAll') && m === 'POST', res: () => okJson({ id: 'draft1' }) },
      { test: (u, m) => u.endsWith('/send') && m === 'POST', res: () => ok2xx(202) },
    ]);
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, ctxWithOutlook());
    const r = await s.tools.outlook_reply.handler({ message_id: 'm1', body: '<p>hi</p>' });
    expect(r.isError).toBeUndefined();
  });

  it('(b) small file (<3 MB): validate → gate → createReply → attach (contentBytes) → send', async () => {
    const fetchMock = router([
      replyGate(),
      { test: (u, m) => u.endsWith('/createReplyAll') && m === 'POST', res: () => okJson({ id: 'draft1' }) },
      { test: (u, m) => u.endsWith('/attachments') && m === 'POST', res: () => ok2xx(201) },
      { test: (u, m) => u.endsWith('/send') && m === 'POST', res: () => ok2xx(202) },
    ]);
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, ctxWithOutlook());
    const r = await s.tools.outlook_reply.handler({ message_id: 'm1', body: '<p>hi</p>', attachments: ['/workspace/file.pdf'] });
    expect(r.isError).toBeUndefined();
    const attachCall = fetchMock.mock.calls.find((c) => String(c[0]).endsWith('/attachments'));
    const body = JSON.parse(attachCall[1].body);
    expect(body['@odata.type']).toBe('#microsoft.graph.fileAttachment');
    expect(body.contentBytes).toBe(Buffer.from('hello world').toString('base64'));
  });

  it('(b2) attach reads the validated realPath, not the raw input path', async () => {
    realpath.mockImplementation(async (p) => (p === '/workspace/file.pdf' ? '/workspace/real/file.pdf' : p));
    const fetchMock = router([
      replyGate(),
      { test: (u, m) => u.endsWith('/createReplyAll') && m === 'POST', res: () => okJson({ id: 'draft1' }) },
      { test: (u, m) => u.endsWith('/attachments') && m === 'POST', res: () => ok2xx(201) },
      { test: (u, m) => u.endsWith('/send') && m === 'POST', res: () => ok2xx(202) },
    ]);
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, ctxWithOutlook());
    const r = await s.tools.outlook_reply.handler({ message_id: 'm1', body: '<p>hi</p>', attachments: ['/workspace/file.pdf'] });
    expect(r.isError).toBeUndefined();
    expect(readFile).toHaveBeenCalledWith('/workspace/real/file.pdf');
  });

  it('(c) large file (≥3 MB): createUploadSession → chunked PUT (no Authorization, redirect:manual) → send', async () => {
    stat.mockResolvedValue({ isFile: () => true, size: SIMPLE });
    readFile.mockResolvedValue(Buffer.alloc(SIMPLE)); // exactly 3 MB → upload session
    const uploadUrl = 'https://outlook.office.com/api/v2.0/uploadSession/xyz';
    const fetchMock = router([
      replyGate(),
      { test: (u, m) => u.endsWith('/createReplyAll') && m === 'POST', res: () => okJson({ id: 'draft1' }) },
      { test: (u) => u.endsWith('/createUploadSession'), res: () => okJson({ uploadUrl }) },
      { test: (u, m) => u === uploadUrl && m === 'PUT', res: () => ok2xx(200) },
      { test: (u, m) => u.endsWith('/send') && m === 'POST', res: () => ok2xx(202) },
    ]);
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, ctxWithOutlook());
    const r = await s.tools.outlook_reply.handler({ message_id: 'm1', body: '<p>hi</p>', attachments: ['/workspace/big.bin'] });
    expect(r.isError).toBeUndefined();
    const putCall = fetchMock.mock.calls.find((c) => c[0] === uploadUrl);
    expect(putCall).toBeDefined();
    expect(isOutlookUploadUrl(putCall[0])).toBe(true);
    expect(putCall[1].headers.Authorization).toBeUndefined();
    expect(putCall[1].redirect).toBe('manual');
    expect(putCall[1].headers['Content-Type']).toBe('application/octet-stream');
  });

  it('(d) a failure after the draft exists DELETEs the draft and returns isError', async () => {
    const fetchMock = router([
      replyGate(),
      { test: (u, m) => u.endsWith('/createReplyAll') && m === 'POST', res: () => okJson({ id: 'draft1' }) },
      { test: (u, m) => u.endsWith('/attachments') && m === 'POST', res: () => failRes(500, 'SECRET') },
      { test: (u, m) => m === 'DELETE', res: () => ok2xx(204) },
    ]);
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, ctxWithOutlook());
    const r = await s.tools.outlook_reply.handler({ message_id: 'm1', body: '<p>hi</p>', attachments: ['/workspace/file.pdf'] });
    expect(r.isError).toBe(true);
    expect(r.content[0].text).not.toContain('SECRET');
    const delCall = fetchMock.mock.calls.find((c) => (c[1]?.method || '').toUpperCase() === 'DELETE');
    expect(delCall).toBeDefined();
    expect(delCall[0]).toContain('/messages/draft1');
  });

  it('(e) out-of-/workspace and oversize attachments are rejected before any Graph call', async () => {
    // out of /workspace
    let fetchMock = router([]);
    vi.stubGlobal('fetch', fetchMock);
    let s = makeServer();
    register(s, ctxWithOutlook());
    let r = await s.tools.outlook_reply.handler({ message_id: 'm1', body: 'x', attachments: ['/etc/passwd'] });
    expect(r.isError).toBe(true);
    expect(fetchMock).not.toHaveBeenCalled();

    // oversize
    stat.mockResolvedValue({ isFile: () => true, size: MAX + 1 });
    fetchMock = router([]);
    vi.stubGlobal('fetch', fetchMock);
    s = makeServer();
    register(s, ctxWithOutlook());
    r = await s.tools.outlook_reply.handler({ message_id: 'm1', body: 'x', attachments: ['/workspace/big.bin'] });
    expect(r.isError).toBe(true);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('(e2) traversal and symlink-escape paths are rejected before any Graph call', async () => {
    realpath.mockImplementation(async (p) => {
      if (p === '/workspace/../etc/passwd') return '/etc/passwd';
      if (p === '/workspace/link') return '/outside/secret';
      return p;
    });
    for (const bad of ['/workspace/../etc/passwd', '/workspace/link']) {
      const fetchMock = router([]);
      vi.stubGlobal('fetch', fetchMock);
      const s = makeServer();
      register(s, ctxWithOutlook());
      const r = await s.tools.outlook_reply.handler({ message_id: 'm1', body: 'x', attachments: [bad] });
      expect(r.isError).toBe(true);
      expect(fetchMock).not.toHaveBeenCalled();
    }
  });
});

// =====================================================================================================
// AC7–AC11 — outlook_send_mail attachments + bcc
// =====================================================================================================
describe('AC7-AC11 outlook_send_mail attachments + bcc', () => {
  it('(AC7) no attachments → exactly one /sendMail POST; bcc carried in the body', async () => {
    const fetchMock = router([{ test: (u) => u.includes('/sendMail'), res: () => ok2xx(202) }]);
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, ctxWithOutlook());
    const r = await s.tools.outlook_send_mail.handler({ to: ['sklep@mycompanystudio.com'], bcc: ['bob@mycompany.com'], subject: 'x', body: 'y' });
    expect(r.isError).toBeUndefined();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toContain('/sendMail');
    const sent = JSON.parse(fetchMock.mock.calls[0][1].body);
    expect(sent.message.bccRecipients[0].emailAddress.address).toBe('bob@mycompany.com');
    expect(sent.message.attachments).toBeUndefined();
  });

  it('(AC8) small file → gate → validate → POST /messages draft → attach → send (202)', async () => {
    const fetchMock = router([
      { test: (u, m) => u.endsWith('/messages') && m === 'POST', res: () => okJson({ id: 'draftS' }) },
      { test: (u, m) => u.endsWith('/attachments') && m === 'POST', res: () => ok2xx(201) },
      { test: (u, m) => u.endsWith('/send') && m === 'POST', res: () => ok2xx(202) },
    ]);
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, ctxWithOutlook());
    const r = await s.tools.outlook_send_mail.handler({ to: ['sklep@mycompanystudio.com'], subject: 'x', body: 'y', attachments: ['/workspace/file.pdf'] });
    expect(r.isError).toBeUndefined();
    expect(fetchMock.mock.calls.some((c) => String(c[0]).includes('/sendMail'))).toBe(false);
    const draftCall = fetchMock.mock.calls.find((c) => String(c[0]).endsWith('/messages') && c[1].method === 'POST');
    expect(JSON.parse(draftCall[1].body).toRecipients[0].emailAddress.address).toBe('sklep@mycompanystudio.com');
  });

  it('(AC9) large file → createUploadSession → PUT (no auth, octet-stream, redirect:manual) → send', async () => {
    stat.mockResolvedValue({ isFile: () => true, size: SIMPLE });
    readFile.mockResolvedValue(Buffer.alloc(SIMPLE));
    const uploadUrl = 'https://outlook.office.com/upload/abc';
    const fetchMock = router([
      { test: (u, m) => u.endsWith('/messages') && m === 'POST', res: () => okJson({ id: 'draftS' }) },
      { test: (u) => u.endsWith('/createUploadSession'), res: () => okJson({ uploadUrl }) },
      { test: (u, m) => u === uploadUrl && m === 'PUT', res: () => ok2xx(200) },
      { test: (u, m) => u.endsWith('/send') && m === 'POST', res: () => ok2xx(202) },
    ]);
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, ctxWithOutlook());
    const r = await s.tools.outlook_send_mail.handler({ to: ['sklep@mycompanystudio.com'], subject: 'x', body: 'y', attachments: ['/workspace/big.bin'] });
    expect(r.isError).toBeUndefined();
    const putCall = fetchMock.mock.calls.find((c) => c[0] === uploadUrl);
    expect(putCall[1].headers.Authorization).toBeUndefined();
    expect(putCall[1].redirect).toBe('manual');
    expect(putCall[1].headers['Content-Type']).toBe('application/octet-stream');
  });

  it('(AC10) a failure after the draft DELETEs the draft and returns a sanitized isError', async () => {
    const fetchMock = router([
      { test: (u, m) => u.endsWith('/messages') && m === 'POST', res: () => okJson({ id: 'draftS' }) },
      { test: (u, m) => u.endsWith('/attachments') && m === 'POST', res: () => failRes(500, 'SECRET') },
      { test: (u, m) => m === 'DELETE', res: () => ok2xx(204) },
    ]);
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, ctxWithOutlook());
    const r = await s.tools.outlook_send_mail.handler({ to: ['sklep@mycompanystudio.com'], subject: 'x', body: 'y', attachments: ['/workspace/file.pdf'] });
    expect(r.isError).toBe(true);
    expect(r.content[0].text).not.toContain('SECRET');
    const delCall = fetchMock.mock.calls.find((c) => (c[1]?.method || '').toUpperCase() === 'DELETE');
    expect(delCall[0]).toContain('/messages/draftS');
  });

  it('(AC11) a non-whitelisted recipient → isError before any Graph call', async () => {
    const fetchMock = router([]);
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, ctxWithOutlook());
    const r = await s.tools.outlook_send_mail.handler({ to: ['sklep@mycompanystudio.com'], bcc: ['stranger@evil.com'], subject: 'x', body: 'y', attachments: ['/workspace/file.pdf'] });
    expect(r.isError).toBe(true);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('(AC11) a bad attachment (whitelisted recipients) → isError before any Graph call', async () => {
    const fetchMock = router([]);
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, ctxWithOutlook());
    const r = await s.tools.outlook_send_mail.handler({ to: ['sklep@mycompanystudio.com'], subject: 'x', body: 'y', attachments: ['/etc/passwd'] });
    expect(r.isError).toBe(true);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

// =====================================================================================================
// R4-F2 — read-time recheck + actual byte length
// =====================================================================================================
describe('R4-F2 read-time recheck', () => {
  it('a file that passes early validation but escapes /workspace at upload time throws → draft DELETEd', async () => {
    // realpath: first resolve (validation) is in-workspace; the read-time re-check resolves outside.
    let calls = 0;
    realpath.mockImplementation(async (p) => {
      if (p === '/workspace/swap.pdf') {
        calls += 1;
        return calls === 1 ? '/workspace/swap.pdf' : '/outside/swap.pdf';
      }
      return p;
    });
    const fetchMock = router([
      { test: (u) => u.includes('$select=from'), res: () => okJson({ from: { emailAddress: { address: 'sklep@mycompanystudio.com' } } }) },
      { test: (u, m) => u.endsWith('/createReplyAll') && m === 'POST', res: () => okJson({ id: 'draftR' }) },
      { test: (u, m) => m === 'DELETE', res: () => ok2xx(204) },
    ]);
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, ctxWithOutlook());
    const r = await s.tools.outlook_reply.handler({ message_id: 'm1', body: 'x', attachments: ['/workspace/swap.pdf'] });
    expect(r.isError).toBe(true);
    // no attach POST/PUT happened; the draft was deleted
    expect(fetchMock.mock.calls.some((c) => String(c[0]).endsWith('/attachments'))).toBe(false);
    expect(fetchMock.mock.calls.some((c) => (c[1]?.method || '').toUpperCase() === 'DELETE')).toBe(true);
  });

  it('the upload session uses the ACTUAL read-time byte length, not the stale validated size', async () => {
    // validation sees a small stat size, but the file has grown to 3 MB by read time → upload session,
    // and createUploadSession.size must equal the real byte length.
    stat.mockResolvedValue({ isFile: () => true, size: 10 });
    readFile.mockResolvedValue(Buffer.alloc(SIMPLE));
    const uploadUrl = 'https://outlook.office.com/upload/abc';
    const fetchMock = router([
      { test: (u) => u.includes('$select=from'), res: () => okJson({ from: { emailAddress: { address: 'sklep@mycompanystudio.com' } } }) },
      { test: (u, m) => u.endsWith('/createReplyAll') && m === 'POST', res: () => okJson({ id: 'draftR' }) },
      { test: (u) => u.endsWith('/createUploadSession'), res: () => okJson({ uploadUrl }) },
      { test: (u, m) => u === uploadUrl && m === 'PUT', res: () => ok2xx(200) },
      { test: (u, m) => u.endsWith('/send') && m === 'POST', res: () => ok2xx(202) },
    ]);
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, ctxWithOutlook());
    const r = await s.tools.outlook_reply.handler({ message_id: 'm1', body: 'x', attachments: ['/workspace/grew.bin'] });
    expect(r.isError).toBeUndefined();
    const sessCall = fetchMock.mock.calls.find((c) => String(c[0]).endsWith('/createUploadSession'));
    expect(JSON.parse(sessCall[1].body).AttachmentItem.size).toBe(SIMPLE);
  });
});
