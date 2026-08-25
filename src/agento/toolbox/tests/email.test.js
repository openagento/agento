import { describe, it, expect, vi, beforeEach } from 'vitest';
// The framework injects the shared toolbox helpers through the registration context
// (config-loader.js TOOLBOX_HELPERS); a module cannot import framework code by path.
import { matchesWhitelist } from '../email-match.js';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const TRANSPORT_PATH = path.resolve(__dirname, '../../modules/core/toolbox/email-transport.js');
const EMAIL_PATH = path.resolve(__dirname, '../../modules/core/toolbox/email.js');

let sendMailMock;
let statMock;

function setupMocks() {
  vi.doMock(TRANSPORT_PATH, () => ({
    createTransporter: () => ({ sendMail: sendMailMock }),
  }));
  vi.doMock('node:fs/promises', () => ({ stat: statMock }));
}

async function buildHandler(coreOverrides = {}) {
  setupMocks();
  const { register } = await import(EMAIL_PATH);
  const log = vi.fn();
  const moduleConfigs = {
    core: {
      smtp_host: 'smtp.test',
      smtp_port: '587',
      smtp_user: 'user@test.com',
      smtp_pass: 'pw',
      smtp_from: 'from@test.com',
      email_whitelist: '*@allowed.com,boss@other.com',
      ...coreOverrides,
    },
  };
  let captured;
  const server = {
    tool(name, desc, schema, handler) {
      captured = { name, desc, schema, handler };
    },
  };
  register(server, { log, moduleConfigs, isToolEnabled: () => true, matchesWhitelist });
  return { handler: captured.handler, schema: captured.schema, log };
}

beforeEach(() => {
  vi.resetModules();
  sendMailMock = vi.fn().mockResolvedValue({ messageId: 'msg-id-1' });
  statMock = vi.fn().mockResolvedValue({ isFile: () => true, size: 1024 });
});

describe('email_send tool', () => {
  it('sends to a single-element to array (joined for nodemailer)', async () => {
    const { handler } = await buildHandler();
    const res = await handler({
      user: 'agent@allowed.com',
      to: ['a@allowed.com'],
      subject: 'hi',
      body_html: '<p>hello</p>',
    });

    expect(res.isError).toBeUndefined();
    expect(sendMailMock).toHaveBeenCalledTimes(1);
    expect(sendMailMock.mock.calls[0][0]).toMatchObject({
      to: 'a@allowed.com',
      subject: 'hi',
      html: '<p>hello</p>',
      replyTo: 'agent@allowed.com',
    });
    expect(sendMailMock.mock.calls[0][0].text).toBeUndefined();
  });

  it('sends to multiple to recipients comma-joined', async () => {
    const { handler } = await buildHandler();
    await handler({
      user: 'agent@allowed.com',
      to: ['a@allowed.com', 'b@allowed.com'],
      subject: 'group',
      body_html: '<p>team</p>',
    });

    expect(sendMailMock).toHaveBeenCalledTimes(1);
    expect(sendMailMock.mock.calls[0][0].to).toBe('a@allowed.com, b@allowed.com');
  });

  it('populates cc and bcc when provided; omits them when absent', async () => {
    const { handler } = await buildHandler();
    await handler({
      user: 'agent@allowed.com',
      to: ['a@allowed.com'],
      cc: ['boss@other.com'],
      bcc: ['c@allowed.com'],
      subject: 's',
      body_html: '<p>b</p>',
    });
    const withCcBcc = sendMailMock.mock.calls[0][0];
    expect(withCcBcc.cc).toBe('boss@other.com');
    expect(withCcBcc.bcc).toBe('c@allowed.com');

    sendMailMock.mockClear();
    await handler({
      user: 'agent@allowed.com',
      to: ['a@allowed.com'],
      subject: 's',
      body_html: '<p>b</p>',
    });
    const noCcBcc = sendMailMock.mock.calls[0][0];
    expect(noCcBcc.cc).toBeUndefined();
    expect(noCcBcc.bcc).toBeUndefined();
  });

  it('rejects the entire send if any recipient (to/cc/bcc) is not whitelisted', async () => {
    const { handler } = await buildHandler();
    const res = await handler({
      user: 'agent@allowed.com',
      to: ['a@allowed.com'],
      cc: ['stranger@evil.com'],
      subject: 's',
      body_html: '<p>b</p>',
    });

    expect(res.isError).toBe(true);
    expect(res.content[0].text).toContain('stranger@evil.com');
    expect(res.content[0].text).toContain('whitelist');
    expect(sendMailMock).not.toHaveBeenCalled();
  });

  it('rejects attachments whose path is not inside /workspace/', async () => {
    const { handler } = await buildHandler();
    const res = await handler({
      user: 'agent@allowed.com',
      to: ['a@allowed.com'],
      subject: 's',
      body_html: '<p>b</p>',
      attachments: ['/etc/passwd'],
    });

    expect(res.isError).toBe(true);
    expect(res.content[0].text).toContain('/workspace/');
    expect(sendMailMock).not.toHaveBeenCalled();
  });

  it('rejects when an attachment file is missing', async () => {
    statMock.mockRejectedValueOnce(Object.assign(new Error('ENOENT'), { code: 'ENOENT' }));
    const { handler } = await buildHandler();
    const res = await handler({
      user: 'agent@allowed.com',
      to: ['a@allowed.com'],
      subject: 's',
      body_html: '<p>b</p>',
      attachments: ['/workspace/missing.pdf'],
    });

    expect(res.isError).toBe(true);
    expect(res.content[0].text).toContain('/workspace/missing.pdf');
    expect(sendMailMock).not.toHaveBeenCalled();
  });

  it('rejects when an attachment exceeds the 25 MB cap', async () => {
    statMock.mockResolvedValueOnce({ isFile: () => true, size: 26 * 1024 * 1024 });
    const { handler } = await buildHandler();
    const res = await handler({
      user: 'agent@allowed.com',
      to: ['a@allowed.com'],
      subject: 's',
      body_html: '<p>b</p>',
      attachments: ['/workspace/big.pdf'],
    });

    expect(res.isError).toBe(true);
    expect(res.content[0].text).toContain('/workspace/big.pdf');
    expect(res.content[0].text).toMatch(/25|too large|exceed/i);
    expect(sendMailMock).not.toHaveBeenCalled();
  });

  it('sends with attachments wired through to nodemailer (filename = basename, path = absolute)', async () => {
    const { handler } = await buildHandler();
    const res = await handler({
      user: 'agent@allowed.com',
      to: ['a@allowed.com'],
      subject: 's',
      body_html: '<p>b</p>',
      attachments: [
        '/workspace/artifacts/ws/av/123/report.pdf',
        '/workspace/artifacts/ws/av/123/chart.png',
      ],
    });

    expect(res.isError).toBeUndefined();
    const payload = sendMailMock.mock.calls[0][0];
    expect(payload.attachments).toEqual([
      { filename: 'report.pdf', path: '/workspace/artifacts/ws/av/123/report.pdf' },
      { filename: 'chart.png', path: '/workspace/artifacts/ws/av/123/chart.png' },
    ]);
    expect(res.content[0].text).toMatch(/2 attachment/);
  });

  it('reports SMTP-not-configured error without calling sendMail', async () => {
    const { handler } = await buildHandler({ smtp_host: null });
    const res = await handler({
      user: 'agent@allowed.com',
      to: ['a@allowed.com'],
      subject: 's',
      body_html: '<p>b</p>',
    });

    expect(res.isError).toBe(true);
    expect(res.content[0].text).toMatch(/SMTP not configured/i);
    expect(sendMailMock).not.toHaveBeenCalled();
  });
});
