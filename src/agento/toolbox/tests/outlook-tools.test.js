import { describe, it, expect, vi, beforeEach } from 'vitest';

import { register, matchesWhitelist } from '../../modules/outlook/toolbox/outlook.js';

// Inject a fake graph-auth so no real @azure/identity is needed; Graph HTTP is stubbed via global fetch.
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

// The read tools surface a message only if it is allow-listed AND passes DMARC, so mocked Graph
// messages that should be readable must carry a passing Authentication-Results header.
const PASS_DMARC = [{ name: 'Authentication-Results', value: 'spf=pass; dkim=pass; dmarc=pass' }];
const FAIL_DMARC = [{ name: 'Authentication-Results', value: 'spf=fail; dmarc=fail' }];

// Response builders (json() for parsed bodies, text() for drain-on-error).
const jsonRes = (obj) => ({ ok: true, json: () => Promise.resolve(obj), text: () => Promise.resolve('') });
const textRes = () => ({ ok: true, text: () => Promise.resolve(''), json: () => Promise.resolve({}) });

function ctx(overrides = {}) {
  return {
    log: vi.fn(),
    moduleConfigs: { outlook: cfg, core: { email_whitelist: 'sklep@mycompanystudio.com, *@mycompany.com' } },
    isToolEnabled: () => true,
    graphAuthFactory,
    ...overrides,
  };
}

// ctx whose outlook config pins an explicit reply_policy ('block' | 'remove').
const ctxWithPolicy = (policy, extra = {}) => ctx({
  moduleConfigs: {
    outlook: { ...cfg, reply_policy: policy },
    core: { email_whitelist: 'sklep@mycompanystudio.com, *@mycompany.com' },
  },
  ...extra,
});

// ctx whose outlook config carries allowed_senders (for the S3 read-restriction tests).
function ctxWithOutlook(outlookOverrides = {}) {
  return {
    log: vi.fn(),
    moduleConfigs: {
      outlook: { ...cfg, allowed_senders: 'sklep@mycompanystudio.com, *@mycompany.com', ...outlookOverrides },
      core: { email_whitelist: 'sklep@mycompanystudio.com, *@mycompany.com' },
    },
    isToolEnabled: () => true,
    graphAuthFactory,
  };
}

// A getCronPool().query mock that returns one page of rows via `const [rows] = await query()`.
const qRows = (rows) => vi.fn(async () => [rows]);

beforeEach(() => vi.unstubAllGlobals());

describe('outlook tools registration + gating', () => {
  it('registers the 5 remaining tools when enabled (enumeration tools removed)', () => {
    const s = makeServer();
    register(s, ctx());
    expect(Object.keys(s.tools).sort()).toEqual([
      'outlook_get_attachment', 'outlook_get_message', 'outlook_mark_processed',
      'outlook_reply', 'outlook_send_mail',
    ]);
  });

  it('does NOT register the removed enumeration tools', () => {
    const s = makeServer();
    register(s, ctx());
    expect(s.tools.outlook_search_messages).toBeUndefined();
    expect(s.tools.outlook_get_new_messages).toBeUndefined();
  });

  it('skips a tool whose is_enabled resolves false (opt-in)', () => {
    const s = makeServer();
    register(s, ctx({ isToolEnabled: (n) => n !== 'outlook_send_mail' }));
    expect(s.tools.outlook_send_mail).toBeUndefined();
    expect(s.tools.outlook_get_message).toBeDefined();
  });

  it('does NOT expose an agent-controlled `user` param (dropped for hardening)', () => {
    const s = makeServer();
    register(s, ctx());
    expect(Object.keys(s.tools.outlook_get_message.schema)).not.toContain('user');
    expect(Object.keys(s.tools.outlook_send_mail.schema)).not.toContain('user');
  });

  it('outlook_send_mail description states it is PREFERRED over email_send (AC13)', () => {
    const s = makeServer();
    register(s, ctx());
    const desc = s.tools.outlook_send_mail.desc;
    expect(desc).toContain('email_send');
    expect(desc).toMatch(/PREFERRED/i);
  });

  it('outlook_reply description states it is reply-all', () => {
    const s = makeServer();
    register(s, ctx());
    expect(s.tools.outlook_reply.desc).toMatch(/reply-all/i);
  });
});

describe('outlook_get_message URL safety', () => {
  it('percent-encodes a message_id containing /, +, = in the Graph URL', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({ subject: 'S', from: { emailAddress: { address: 'a@b.com' } } }) });
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, ctx());
    await s.tools.outlook_get_message.handler({ message_id: 'AA/BB+CC=DD' });
    const url = fetchMock.mock.calls[0][0];
    expect(url).toContain('AA%2FBB%2BCC%3DDD');
    expect(url).not.toContain('AA/BB+CC=DD');
  });
});

describe('outlook_reply = reply-all (reply_policy=block → block-whole whitelist gate)', () => {
  // Delivered set = (replyTo || from) ∪ to ∪ cc, minus the agent's own mailbox. Under the explicit
  // 'block' policy EVERY address must be whitelisted or the whole send is blocked with no Graph mutation
  // (mirrors outlook_send_mail). The DEFAULT policy is 'remove' — covered in the next describe.
  it('BLOCKS when the original sender is not whitelisted (no createReplyAll issued)', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(jsonRes({ from: { emailAddress: { address: 'stranger@evil.com' } } }));
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, ctxWithPolicy('block'));
    const r = await s.tools.outlook_reply.handler({ message_id: 'm1', body: 'hi' });
    expect(r.isError).toBe(true);
    // only the metadata lookup happened; no createReplyAll
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toContain('$select=from,replyTo,toRecipients,ccRecipients,conversationId');
    expect(fetchMock.mock.calls.some((c) => String(c[0]).endsWith('/createReplyAll'))).toBe(false);
  });

  it('BLOCKS the whole send when ONE of many recipients is not whitelisted (no createReplyAll)', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(jsonRes({
      from: { emailAddress: { address: 'sklep@mycompanystudio.com' } },
      toRecipients: [{ emailAddress: { address: 'bob@mycompany.com' } }],
      ccRecipients: [{ emailAddress: { address: 'stranger@evil.com' } }],
    }));
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, ctxWithPolicy('block'));
    const r = await s.tools.outlook_reply.handler({ message_id: 'm1', body: 'hi' });
    expect(r.isError).toBe(true);
    expect(r.content[0].text).toContain('stranger@evil.com');
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('ALLOWS reply-all when every recipient is whitelisted (createReplyAll draft → send, no PATCH)', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonRes({
        from: { emailAddress: { address: 'sklep@mycompanystudio.com' } },
        toRecipients: [{ emailAddress: { address: 'bob@mycompany.com' } }],
      }))
      .mockResolvedValueOnce(jsonRes({ id: 'draft1' }))  // createReplyAll
      .mockResolvedValueOnce(textRes());                 // send
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, ctxWithPolicy('block'));
    const r = await s.tools.outlook_reply.handler({ message_id: 'm1', body: '<p>hi</p>' });
    expect(r.isError).toBeUndefined();
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(fetchMock.mock.calls[1][0]).toMatch(/\/createReplyAll$/);
    const draft = JSON.parse(fetchMock.mock.calls[1][1].body);
    expect(draft.message.body.contentType).toBe('HTML');
    expect(draft.message.body.content).toBe('<p>hi</p>');
    expect(fetchMock.mock.calls[2][0]).toMatch(/\/send$/);
  });

  it('excludes the agent own mailbox from the delivered set (self on To is not gated)', async () => {
    // The agent (agent@example.com) is on To; only sklep + bob remain. All whitelisted → send proceeds.
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonRes({
        from: { emailAddress: { address: 'sklep@mycompanystudio.com' } },
        toRecipients: [
          { emailAddress: { address: 'agent@example.com' } },
          { emailAddress: { address: 'bob@mycompany.com' } },
        ],
      }))
      .mockResolvedValueOnce(jsonRes({ id: 'draft1' }))
      .mockResolvedValueOnce(textRes());
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, ctxWithPolicy('block'));
    const r = await s.tools.outlook_reply.handler({ message_id: 'm1', body: '<p>hi</p>' });
    expect(r.isError).toBeUndefined();
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it('honors Reply-To for the delivered set (non-whitelisted Reply-To blocks the whole send)', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(jsonRes({
      from: { emailAddress: { address: 'sklep@mycompanystudio.com' } },
      replyTo: [{ emailAddress: { address: 'stranger@evil.com' } }],
    }));
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, ctxWithPolicy('block'));
    const r = await s.tools.outlook_reply.handler({ message_id: 'm1', body: 'hi' });
    expect(r.isError).toBe(true);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe('outlook_reply = reply-all (reply_policy=remove → drop blocked recipients; DEFAULT)', () => {
  // The default policy drops any recipient not in core/email_whitelist and sends to the rest, so one bad
  // address in a group thread never blocks the whole conversation. Mail STILL only reaches whitelisted
  // addresses. Flow when there are drops: meta → createReplyAll → PATCH recipients → send.
  it('DEFAULT (no reply_policy set) resolves to remove: drops the blocked recipient and sends', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonRes({
        from: { emailAddress: { address: 'sklep@mycompanystudio.com' } },
        toRecipients: [{ emailAddress: { address: 'bob@mycompany.com' } }],
        ccRecipients: [{ emailAddress: { address: 'stranger@evil.com' } }],
      }))
      .mockResolvedValueOnce(jsonRes({ id: 'draft1' }))  // createReplyAll
      .mockResolvedValueOnce(textRes())                  // PATCH recipients
      .mockResolvedValueOnce(textRes());                 // send
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, ctx()); // no reply_policy in cfg → default 'remove'
    const r = await s.tools.outlook_reply.handler({ message_id: 'm1', body: '<p>hi</p>' });
    expect(r.isError).toBeUndefined();
    expect(fetchMock).toHaveBeenCalledTimes(4);
    expect(fetchMock.mock.calls[1][0]).toMatch(/\/createReplyAll$/);
    const patch = fetchMock.mock.calls[2];
    expect(patch[1].method).toBe('PATCH');
    const patchBody = JSON.parse(patch[1].body);
    const addrs = [...(patchBody.toRecipients || []), ...(patchBody.ccRecipients || [])]
      .map((x) => x.emailAddress.address.toLowerCase());
    expect(addrs).toContain('sklep@mycompanystudio.com');
    expect(addrs).toContain('bob@mycompany.com');
    expect(addrs).not.toContain('stranger@evil.com');
    expect(fetchMock.mock.calls[3][0]).toMatch(/\/send$/);
    // The agent is told exactly who was omitted (not silent).
    expect(r.content[0].text).toContain('stranger@evil.com');
  });

  it('preserves reply-all buckets: original sender → To, surviving To/Cc → Cc', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonRes({
        from: { emailAddress: { address: 'sklep@mycompanystudio.com' } },
        toRecipients: [{ emailAddress: { address: 'anna@mycompany.com' } }],
        ccRecipients: [
          { emailAddress: { address: 'piotr@mycompany.com' } },
          { emailAddress: { address: 'stranger@evil.com' } },
        ],
      }))
      .mockResolvedValueOnce(jsonRes({ id: 'd1' }))
      .mockResolvedValueOnce(textRes())
      .mockResolvedValueOnce(textRes());
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, ctx());
    await s.tools.outlook_reply.handler({ message_id: 'm1', body: '<p>hi</p>' });
    const patchBody = JSON.parse(fetchMock.mock.calls[2][1].body);
    const to = patchBody.toRecipients.map((x) => x.emailAddress.address.toLowerCase());
    const cc = patchBody.ccRecipients.map((x) => x.emailAddress.address.toLowerCase());
    expect(to).toEqual(['sklep@mycompanystudio.com']);
    expect(cc).toEqual(['anna@mycompany.com', 'piotr@mycompany.com']);
    expect([...to, ...cc]).not.toContain('stranger@evil.com');
  });

  it('all recipients whitelisted → no drop, no PATCH (meta → createReplyAll → send)', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonRes({
        from: { emailAddress: { address: 'sklep@mycompanystudio.com' } },
        toRecipients: [{ emailAddress: { address: 'bob@mycompany.com' } }],
      }))
      .mockResolvedValueOnce(jsonRes({ id: 'd1' }))
      .mockResolvedValueOnce(textRes());
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, ctx());
    const r = await s.tools.outlook_reply.handler({ message_id: 'm1', body: '<p>hi</p>' });
    expect(r.isError).toBeUndefined();
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(fetchMock.mock.calls.some((c) => c[1]?.method === 'PATCH')).toBe(false);
  });

  it('EVERY recipient blocked → nothing sent, errors, no createReplyAll (cannot reply to nobody)', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(jsonRes({
      from: { emailAddress: { address: 'stranger@evil.com' } },
      toRecipients: [{ emailAddress: { address: 'other@evil.com' } }],
    }));
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, ctx());
    const r = await s.tools.outlook_reply.handler({ message_id: 'm1', body: 'hi' });
    expect(r.isError).toBe(true);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls.some((c) => String(c[0]).endsWith('/createReplyAll'))).toBe(false);
  });
});

describe('outlook_send_mail recipient whitelist', () => {
  it('BLOCKS sending to a non-whitelisted recipient (no sendMail issued)', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, ctx());
    const r = await s.tools.outlook_send_mail.handler({ to: ['stranger@evil.com'], subject: 'x', body: 'y' });
    expect(r.isError).toBe(true);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe('current-job read binding (privacy-by-construction for headless jobs)', () => {
  const okMsg = (addr, headers = PASS_DMARC) => jsonRes({ subject: 'S', from: { emailAddress: { address: addr } }, internetMessageHeaders: headers });
  const okGate = (addr, headers = PASS_DMARC) => jsonRes({ from: { emailAddress: { address: addr } }, internetMessageHeaders: headers });

  function ctxJob(jobId, query, agentViewId = 5) {
    return { ...ctxWithOutlook(), db: { getCronPool: () => ({ query }) }, jobId, agentViewId };
  }

  it('get_message on the job own triggering id is allowed; query is scoped to agent_view + outlook source', async () => {
    const query = qRows([{ reference_id: 'some-subject-slug::MSG1' }]);
    const fetchMock = vi.fn().mockResolvedValue(okMsg('sklep@mycompanystudio.com'));
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, ctxJob(10, query));
    const r = await s.tools.outlook_get_message.handler({ message_id: 'MSG1' });
    expect(r.isError).toBeUndefined();
    // Scope-checked WHERE + bound params.
    expect(query.mock.calls[0][0]).toContain('agent_view_id = ?');
    expect(query.mock.calls[0][0]).toContain("source = 'outlook'");
    expect(query.mock.calls[0][1]).toEqual([10, 5]);
  });

  it('get_message on a DIFFERENT id returns a generic isError and issues NO Graph call', async () => {
    const query = qRows([{ reference_id: 'slug::MSG1' }]);
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, ctxJob(10, query));
    const r = await s.tools.outlook_get_message.handler({ message_id: 'OTHER_ID' });
    expect(r.isError).toBe(true);
    expect(r.content[0].text).toBe('Error: message is not available for this task.');
    expect(r.content[0].text).not.toContain('MSG1'); // no leak of the bound id
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('jobId for another agent_view (no scoped row) → fail closed (serve nothing)', async () => {
    const query = qRows([]); // WHERE agent_view_id/source excludes it → no row
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, ctxJob(10, query));
    const r = await s.tools.outlook_get_message.handler({ message_id: 'MSG1' });
    expect(r.isError).toBe(true);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('jobId for a non-Outlook source (no scoped row) → fail closed', async () => {
    const query = qRows([]); // source='outlook' filter excludes a non-outlook job
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, ctxJob(10, query));
    const r = await s.tools.outlook_get_message.handler({ message_id: 'MSG1' });
    expect(r.isError).toBe(true);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('jobId null (interactive escape hatch) → any id allowed, no DB lookup', async () => {
    const query = vi.fn();
    const fetchMock = vi.fn().mockResolvedValue(okMsg('sklep@mycompanystudio.com'));
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, ctxJob(null, query));
    const r = await s.tools.outlook_get_message.handler({ message_id: 'ANYTHING' });
    expect(r.isError).toBeUndefined();
    expect(query).not.toHaveBeenCalled(); // binding disabled → never queries the job table
  });

  it('get_attachment on the job own id is allowed; on a different id it is refused (no Graph call)', async () => {
    // allowed: gate GET → meta → $value (write path itself is covered by outlook-attachments.test.js)
    const okMeta = jsonRes({ id: 'att1', name: 'r.pdf', contentType: 'application/pdf', size: 2, '@odata.type': '#microsoft.graph.fileAttachment' });
    const okValue = { ok: true, arrayBuffer: async () => new Uint8Array([1, 2]).buffer, headers: { get: () => null }, text: async () => '' };
    const query = qRows([{ reference_id: 'slug::MSG1' }]);
    const fetchAllowed = vi.fn()
      .mockResolvedValueOnce(okGate('sklep@mycompanystudio.com'))
      .mockResolvedValueOnce(okMeta)
      .mockResolvedValueOnce(okValue);
    vi.stubGlobal('fetch', fetchAllowed);
    let s = makeServer();
    register(s, { ...ctxJob(10, query), artifactsDir: '/workspace/artifacts/ws/av/10' });
    const rOwn = await s.tools.outlook_get_attachment.handler({ message_id: 'MSG1', attachment_id: 'att1' });
    expect(fetchAllowed).toHaveBeenCalled(); // gate GET issued → binding allowed the own id
    expect(rOwn).toBeDefined();

    const query2 = qRows([{ reference_id: 'slug::MSG1' }]);
    const fetchDenied = vi.fn();
    vi.stubGlobal('fetch', fetchDenied);
    s = makeServer();
    register(s, ctxJob(10, query2));
    const rOther = await s.tools.outlook_get_attachment.handler({ message_id: 'OTHER', attachment_id: 'att1' });
    expect(rOther.isError).toBe(true);
    expect(rOther.content[0].text).toBe('Error: message is not available for this task.');
    expect(fetchDenied).not.toHaveBeenCalled();
  });

  it('outlook_reply on a DIFFERENT id is refused before ANY Graph call (no reply-all into another thread)', async () => {
    const query = qRows([{ reference_id: 'slug::MSG1' }]);
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, ctxJob(10, query));
    const r = await s.tools.outlook_reply.handler({ message_id: 'OTHER', body: 'hi' });
    expect(r.isError).toBe(true);
    expect(r.content[0].text).toBe('Error: message is not available for this task.');
    expect(fetchMock).not.toHaveBeenCalled(); // no metadata lookup, no createReplyAll
  });

  it('outlook_reply on the job own id proceeds to createReplyAll', async () => {
    const query = qRows([{ reference_id: 'slug::MSG1' }]);
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonRes({ from: { emailAddress: { address: 'sklep@mycompanystudio.com' } }, toRecipients: [], ccRecipients: [] }))
      .mockResolvedValueOnce(jsonRes({ id: 'd1' }))
      .mockResolvedValueOnce(textRes());
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, ctxJob(10, query));
    const r = await s.tools.outlook_reply.handler({ message_id: 'MSG1', body: '<p>hi</p>' });
    expect(r.isError).toBeUndefined();
    expect(fetchMock.mock.calls.some((c) => String(c[0]).endsWith('/createReplyAll'))).toBe(true);
  });

  it('outlook_mark_processed on a DIFFERENT id is refused before the PATCH', async () => {
    const query = qRows([{ reference_id: 'slug::MSG1' }]);
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, ctxJob(10, query));
    const r = await s.tools.outlook_mark_processed.handler({ message_id: 'OTHER' });
    expect(r.isError).toBe(true);
    expect(r.content[0].text).toBe('Error: message is not available for this task.');
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('outlook_mark_processed on the job own id issues the isRead PATCH', async () => {
    const query = qRows([{ reference_id: 'slug::MSG1' }]);
    const fetchMock = vi.fn().mockResolvedValue(textRes());
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, ctxJob(10, query));
    const r = await s.tools.outlook_mark_processed.handler({ message_id: 'MSG1' });
    expect(r.isError).toBeUndefined();
    expect((fetchMock.mock.calls[0][1].method || '').toUpperCase()).toBe('PATCH');
  });
});

describe('MCP tools target the per-agent_view mailbox (no code change — scoped moduleConfigs)', () => {
  // The toolbox scopes a session's moduleConfigs to the job's agent_view_id (registerTools ->
  // loadScopedDbOverrides). graph-auth derives the mailbox from the config it is built with, so the
  // mailbox in moduleConfigs.outlook is the one the Graph URL hits. Mirror that here.
  const scopedAuthFactory = (c) => ({
    isConfigured: () => true,
    getToken: async () => 'AAA',
    getMailboxUserId: () => c.outlook_mailbox_user_id,
  });

  function ctxForView(mailbox) {
    return {
      log: vi.fn(),
      moduleConfigs: {
        outlook: { ...cfg, outlook_mailbox_user_id: mailbox },
        core: { email_whitelist: 'sklep@mycompanystudio.com, *@mycompany.com' },
      },
      isToolEnabled: () => true,
      graphAuthFactory: scopedAuthFactory,
    };
  }

  it('outlook_reply for a job under view X uses view X mailbox; view Y uses view Y mailbox', async () => {
    const fetchMock = vi.fn()
      // view X: meta lookup (whitelisted) → createReplyAll → send
      .mockResolvedValueOnce(jsonRes({ from: { emailAddress: { address: 'sklep@mycompanystudio.com' } } }))
      .mockResolvedValueOnce(jsonRes({ id: 'dX' }))
      .mockResolvedValueOnce(textRes())
      // view Y: same
      .mockResolvedValueOnce(jsonRes({ from: { emailAddress: { address: 'sklep@mycompanystudio.com' } } }))
      .mockResolvedValueOnce(jsonRes({ id: 'dY' }))
      .mockResolvedValueOnce(textRes());
    vi.stubGlobal('fetch', fetchMock);

    const sx = makeServer();
    register(sx, ctxForView('viewx@example.com'));
    await sx.tools.outlook_reply.handler({ message_id: 'mX', body: 'hi' });

    const sy = makeServer();
    register(sy, ctxForView('viewy@example.com'));
    await sy.tools.outlook_reply.handler({ message_id: 'mY', body: 'hi' });

    // calls[1] is view X's createReplyAll, calls[4] is view Y's createReplyAll
    expect(fetchMock.mock.calls[1][0]).toContain('/users/viewx%40example.com/');
    expect(fetchMock.mock.calls[4][0]).toContain('/users/viewy%40example.com/');
  });

  it('outlook_mark_processed PATCHes the per-view mailbox message', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, text: () => Promise.resolve('') });
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, ctxForView('viewz@example.com'));
    await s.tools.outlook_mark_processed.handler({ message_id: 'm1' });
    expect(fetchMock.mock.calls[0][0]).toContain('/users/viewz%40example.com/messages/');
    expect(fetchMock.mock.calls[0][1].method).toBe('PATCH');
  });
});

describe('S1: Graph error bodies are sanitized (no provider internals leak to the agent)', () => {
  it('outlook_get_message returns a status-only error, never the raw Graph body', async () => {
    const leak = 'SECRET mailbox=victim@corp.com tenant=11111111-2222 x-ms-diagnostics=internal-detail';
    const fetchMock = vi.fn().mockResolvedValue({ ok: false, status: 404, text: () => Promise.resolve(leak) });
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, ctxWithOutlook());
    const r = await s.tools.outlook_get_message.handler({ message_id: 'm1' });
    expect(r.isError).toBe(true);
    const text = r.content[0].text;
    expect(text).toContain('404');
    expect(text).not.toContain('SECRET');
    expect(text).not.toContain('victim@corp.com');
    expect(text).not.toContain('x-ms-diagnostics');
  });

  it('outlook_send_mail returns a status-only error on a Graph failure', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: false, status: 500, text: () => Promise.resolve('SECRET tenant detail') });
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, ctxWithOutlook());
    const r = await s.tools.outlook_send_mail.handler({ to: ['sklep@mycompanystudio.com'], subject: 'x', body: 'y' });
    expect(r.isError).toBe(true);
    expect(r.content[0].text).toContain('500');
    expect(r.content[0].text).not.toContain('SECRET');
  });
});

describe('S3: read tools are restricted to allowed_senders (default on)', () => {
  const okMsg = (addr, headers = PASS_DMARC) => ({ ok: true, json: () => Promise.resolve({ subject: 'S', from: { emailAddress: { address: addr } }, internetMessageHeaders: headers }) });

  it('outlook_get_message BLOCKS (and withholds the body of) a non-allow-listed sender', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(okMsg('stranger@evil.com')));
    const s = makeServer();
    register(s, ctxWithOutlook());
    const r = await s.tools.outlook_get_message.handler({ message_id: 'm1' });
    expect(r.isError).toBe(true);
    expect(r.content[0].text).not.toContain('"subject"'); // result object never serialized
  });

  it('outlook_get_message ALLOWS an allow-listed sender', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(okMsg('sklep@mycompanystudio.com')));
    const s = makeServer();
    register(s, ctxWithOutlook());
    const r = await s.tools.outlook_get_message.handler({ message_id: 'm1' });
    expect(r.isError).toBeUndefined();
    expect(r.content[0].text).toContain('sklep@mycompanystudio.com');
  });

  it('outlook_get_message returns any sender when the restriction is disabled', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(okMsg('stranger@evil.com')));
    const s = makeServer();
    register(s, ctxWithOutlook({ restrict_read_to_allowed_senders: false }));
    const r = await s.tools.outlook_get_message.handler({ message_id: 'm1' });
    expect(r.isError).toBeUndefined();
    expect(r.content[0].text).toContain('stranger@evil.com');
  });

  it('empty allowed_senders blocks all reads (fail-closed) while restriction is on', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(okMsg('sklep@mycompanystudio.com')));
    const s = makeServer();
    register(s, ctxWithOutlook({ allowed_senders: '' }));
    const r = await s.tools.outlook_get_message.handler({ message_id: 'm1' });
    expect(r.isError).toBe(true);
  });

  // --- DMARC gate: an allow-listed From is forgeable; the read tools require a DMARC pass too,
  //     mirroring the publisher, so a spoofed allow-listed sender can't be read (prompt-injection). ---

  it('ANTI-SPOOF: outlook_get_message BLOCKS an allow-listed sender that FAILS DMARC', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(okMsg('sklep@mycompanystudio.com', FAIL_DMARC)));
    const s = makeServer();
    register(s, ctxWithOutlook());
    const r = await s.tools.outlook_get_message.handler({ message_id: 'm1' });
    expect(r.isError).toBe(true);
    expect(r.content[0].text).not.toContain('"subject"'); // body withheld
  });

  it('FAIL-CLOSED: outlook_get_message BLOCKS an allow-listed sender with NO DMARC header', async () => {
    // No internetMessageHeaders at all → verdict undeterminable → not surfaced.
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({ subject: 'S', from: { emailAddress: { address: 'sklep@mycompanystudio.com' } } }) }));
    const s = makeServer();
    register(s, ctxWithOutlook());
    const r = await s.tools.outlook_get_message.handler({ message_id: 'm1' });
    expect(r.isError).toBe(true);
  });

  it('read tools request internetMessageHeaders so DMARC can be evaluated', async () => {
    const fetchMock = vi.fn().mockResolvedValue(okMsg('sklep@mycompanystudio.com'));
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, ctxWithOutlook());
    await s.tools.outlook_get_message.handler({ message_id: 'm1' });
    expect(fetchMock.mock.calls[0][0]).toContain('internetMessageHeaders');
  });

  it('restriction OFF bypasses the DMARC gate too (documented security risk)', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(okMsg('stranger@evil.com', FAIL_DMARC)));
    const s = makeServer();
    register(s, ctxWithOutlook({ restrict_read_to_allowed_senders: false }));
    const r = await s.tools.outlook_get_message.handler({ message_id: 'm1' });
    expect(r.isError).toBeUndefined();
  });
});

describe('matchesWhitelist semantics (S2: full metachar escaping)', () => {
  it('empty whitelist blocks all (fail-closed)', () => {
    expect(matchesWhitelist('anyone@x.com', [])).toBe(false);
  });

  it('wildcard matches any local part but never crosses @', () => {
    const wl = ['*@mycompany.com'];
    expect(matchesWhitelist('bob@mycompany.com', wl)).toBe(true);
    expect(matchesWhitelist('evil@sub.mycompany.com', wl)).toBe(false);
  });

  it('is case-insensitive', () => {
    expect(matchesWhitelist('Bob@Mycompany.com', ['bob@mycompany.com'])).toBe(true);
  });

  it("escapes '?' (literal, not a quantifier) — closes the fail-open widening", () => {
    const wl = ['a?b@x.com'];
    expect(matchesWhitelist('a?b@x.com', wl)).toBe(true);
    expect(matchesWhitelist('b@x.com', wl)).toBe(false);
  });

  it('escapes regex metachars in the local part', () => {
    expect(matchesWhitelist('a.b+c@x.com', ['a.b+c@x.com'])).toBe(true);
    expect(matchesWhitelist('axbxc@x.com', ['a.b+c@x.com'])).toBe(false);
  });
});

describe('outlook_send_mail allow path + cc', () => {
  it('sends when every to/cc recipient is whitelisted (POST body carries to+cc)', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, text: () => Promise.resolve('') });
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, ctx());
    const r = await s.tools.outlook_send_mail.handler({
      to: ['sklep@mycompanystudio.com'], cc: ['bob@mycompany.com'], subject: 'x', body: 'y',
    });
    expect(r.isError).toBeUndefined();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toContain('/sendMail');
    const sent = JSON.parse(fetchMock.mock.calls[0][1].body);
    expect(sent.message.toRecipients[0].emailAddress.address).toBe('sklep@mycompanystudio.com');
    expect(sent.message.ccRecipients[0].emailAddress.address).toBe('bob@mycompany.com');
    expect(sent.message.body.contentType).toBe('HTML'); // sent as HTML, not plain text
  });

  it('blocks the whole send when any cc recipient is not whitelisted', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, ctx());
    const r = await s.tools.outlook_send_mail.handler({
      to: ['sklep@mycompanystudio.com'], cc: ['stranger@evil.com'], subject: 'x', body: 'y',
    });
    expect(r.isError).toBe(true);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe('tools fail closed when Graph is not configured', () => {
  const unconfigured = () => ({ isConfigured: () => false, getToken: async () => 'AAA', getMailboxUserId: () => 'agent@example.com' });
  for (const name of [
    'outlook_get_message', 'outlook_get_attachment', 'outlook_reply', 'outlook_send_mail', 'outlook_mark_processed',
  ]) {
    it(`${name} returns isError and issues no Graph call when not configured`, async () => {
      const fetchMock = vi.fn();
      vi.stubGlobal('fetch', fetchMock);
      const s = makeServer();
      register(s, ctx({ graphAuthFactory: unconfigured }));
      const r = await s.tools[name].handler({ message_id: 'm1', attachment_id: 'a1', body: 'b', subject: 's', to: ['sklep@mycompanystudio.com'] });
      expect(r.isError).toBe(true);
      expect(fetchMock).not.toHaveBeenCalled();
    });
  }
});

describe('outlook thread read (allow_thread_read) — read scope follows the thread, actions stay bound', () => {
  // Context: headless job whose trigger message is TRIG, feature ON. The binding row resolves the
  // trigger message id from reference_id, exactly like the current-job binding tests above.
  function threadCtx(outlookOverrides = {}) {
    return {
      log: vi.fn(),
      moduleConfigs: {
        outlook: { ...cfg, allowed_senders: 'sklep@mycompanystudio.com, *@mycompany.com', allow_thread_read: true, ...outlookOverrides },
        core: { email_whitelist: 'sklep@mycompanystudio.com, *@mycompany.com' },
      },
      isToolEnabled: () => true,
      graphAuthFactory, // mailbox agent@example.com
      db: { getCronPool: () => ({ query: qRows([{ reference_id: 'slug::TRIG' }]) }) },
      jobId: 10,
      agentViewId: 5,
      artifactsDir: '/workspace/artifacts/ws/av/10',
    };
  }

  // Same conversation as TRIG. IN1 (allowed inbound) + OUT1 (trusted Sent-Items outbound) are authorized;
  // INEVIL (sender off allow-list), SPOOF (allow-listed From but DMARC fail) and FORGE (From == agent
  // mailbox but NOT in Sent Items) are all rejected.
  const CONV_LIST = [
    { id: 'IN1', conversationId: 'CONV1', subject: 'Audyt', from: { emailAddress: { address: 'sklep@mycompanystudio.com' } }, receivedDateTime: '2026-09-15T11:07:00Z', hasAttachments: false, parentFolderId: 'INBOX', internetMessageHeaders: PASS_DMARC },
    { id: 'OUT1', conversationId: 'CONV1', subject: 'Re: Audyt', from: { emailAddress: { address: 'agent@example.com' } }, receivedDateTime: '2026-09-15T11:19:00Z', hasAttachments: true, parentFolderId: 'SENT', internetMessageHeaders: [] },
    { id: 'INEVIL', conversationId: 'CONV1', subject: 'x', from: { emailAddress: { address: 'stranger@evil.com' } }, receivedDateTime: '2026-09-15T11:30:00Z', hasAttachments: false, parentFolderId: 'INBOX', internetMessageHeaders: PASS_DMARC },
    { id: 'SPOOF', conversationId: 'CONV1', subject: 'x', from: { emailAddress: { address: 'sklep@mycompanystudio.com' } }, receivedDateTime: '2026-09-15T11:40:00Z', hasAttachments: false, parentFolderId: 'INBOX', internetMessageHeaders: FAIL_DMARC },
    { id: 'FORGE', conversationId: 'CONV1', subject: 'x', from: { emailAddress: { address: 'agent@example.com' } }, receivedDateTime: '2026-09-15T11:50:00Z', hasAttachments: false, parentFolderId: 'INBOX', internetMessageHeaders: [] },
  ];
  const CONV_ATT = { OUT1: [{ id: 'ATT1', name: 'r.csv', contentType: 'text/csv', size: 3 }] };

  // URL-routing Graph mock (the thread flow branches, so ordered mockResolvedValueOnce is unwieldy).
  function makeThreadFetch({ conversationId = 'CONV1', sentId = 'SENT', list = CONV_LIST, attachments = CONV_ATT, fullMessages = {}, nextLink = false } = {}) {
    return vi.fn(async (url) => {
      const u = String(url);
      if (/\/messages\/TRIG\?\$select=id,conversationId/.test(u)) return jsonRes(conversationId ? { id: 'TRIG', conversationId } : { id: 'TRIG' });
      if (/\/mailFolders\/sentitems/.test(u)) return jsonRes({ id: sentId });
      if (/\/messages\?\$filter=/.test(u)) return jsonRes({ value: list, ...(nextLink ? { '@odata.nextLink': 'https://graph/next' } : {}) });
      if (/\/attachments\/[^/]+\/\$value/.test(u)) return { ok: true, arrayBuffer: async () => new Uint8Array([1, 2, 3]).buffer, headers: { get: () => null }, text: async () => '' };
      let m = u.match(/\/messages\/([^/]+)\/attachments\/([^/?]+)\?\$select=id,name/);
      if (m) return jsonRes({ id: decodeURIComponent(m[2]), name: 'r.csv', contentType: 'text/csv', size: 3, '@odata.type': '#microsoft.graph.fileAttachment' });
      m = u.match(/\/messages\/([^/]+)\/attachments\?\$select=id,name/);
      if (m) return jsonRes({ value: attachments[decodeURIComponent(m[1])] || [] });
      m = u.match(/\/messages\/([^/?]+)\?\$select=subject/);
      if (m) return jsonRes(fullMessages[decodeURIComponent(m[1])] || { subject: 'S', from: { emailAddress: { address: 'sklep@mycompanystudio.com' } }, internetMessageHeaders: PASS_DMARC });
      return jsonRes({});
    });
  }

  it('T1: feature OFF → outlook_list_thread is NOT registered', () => {
    const s = makeServer();
    register(s, threadCtx({ allow_thread_read: false }));
    expect(s.tools.outlook_list_thread).toBeUndefined();
  });

  it('feature ON → outlook_list_thread IS registered', () => {
    const s = makeServer();
    register(s, threadCtx());
    expect(s.tools.outlook_list_thread).toBeDefined();
  });

  it('list_thread returns ONLY authorized messages, oldest→newest, metadata only (no bodies/bytes)', async () => {
    vi.stubGlobal('fetch', makeThreadFetch());
    const s = makeServer();
    register(s, threadCtx());
    const r = await s.tools.outlook_list_thread.handler({});
    expect(r.isError).toBeUndefined();
    const out = JSON.parse(r.content[0].text);
    // INEVIL (T4), SPOOF (T5), FORGE (T7) all filtered out; order oldest→newest.
    expect(out.messages.map((m) => m.message_id)).toEqual(['IN1', 'OUT1']);
    expect(out.messages[0].direction).toBe('inbound');
    expect(out.messages[1].direction).toBe('outbound'); // T6: trusted Sent-Items outbound
    expect(out.messages[1].attachments).toEqual([{ attachment_id: 'ATT1', name: 'r.csv', content_type: 'text/csv', size: 3 }]); // T12
    expect(out.truncated).toBe(false);
    expect(r.content[0].text).not.toContain('contentBytes');
    expect(r.content[0].text).not.toContain('conversationId'); // conversation id never exposed to the agent
  });

  it('T11: conversation exceeds the limit → truncated=true', async () => {
    vi.stubGlobal('fetch', makeThreadFetch({ nextLink: true }));
    const s = makeServer();
    register(s, threadCtx());
    const out = JSON.parse((await s.tools.outlook_list_thread.handler({})).content[0].text);
    expect(out.truncated).toBe(true);
  });

  it('T2: get_message on the trigger still works (surface gate applies)', async () => {
    vi.stubGlobal('fetch', makeThreadFetch());
    const s = makeServer();
    register(s, threadCtx());
    const r = await s.tools.outlook_get_message.handler({ message_id: 'TRIG' });
    expect(r.isError).toBeUndefined();
  });

  it('T3: get_message on an allowed inbound thread message works', async () => {
    vi.stubGlobal('fetch', makeThreadFetch());
    const s = makeServer();
    register(s, threadCtx());
    const r = await s.tools.outlook_get_message.handler({ message_id: 'IN1' });
    expect(r.isError).toBeUndefined();
    expect(r.content[0].text).toContain('sklep@mycompanystudio.com');
  });

  it('T6: get_message on a trusted OUTBOUND thread message works even though From == agent mailbox (surface gate skipped for authorized thread messages)', async () => {
    // A full GET of OUT1 returns the agent mailbox + no DMARC — surfaceAllowed would BLOCK it; the thread
    // binding already authorized it (physically in Sent Items), so the read is allowed.
    const fullMessages = { OUT1: { subject: 'Re: Audyt', from: { emailAddress: { address: 'agent@example.com' } }, hasAttachments: true, internetMessageHeaders: [] } };
    vi.stubGlobal('fetch', makeThreadFetch({ fullMessages }));
    const s = makeServer();
    register(s, threadCtx());
    const r = await s.tools.outlook_get_message.handler({ message_id: 'OUT1' });
    expect(r.isError).toBeUndefined();
    expect(r.content[0].text).toContain('agent@example.com');
  });

  it('T6: get_attachment on the trusted outbound message skips the surface gate GET and proceeds to download', async () => {
    const fetchMock = makeThreadFetch();
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, threadCtx());
    const r = await s.tools.outlook_get_attachment.handler({ message_id: 'OUT1', attachment_id: 'ATT1' });
    expect(r.content[0].text).not.toBe('Error: message is not available for this task.');
    // The from,internetMessageHeaders gate GET is never issued for an authorized thread message.
    expect(fetchMock.mock.calls.every((c) => !/\?\$select=from,internetMessageHeaders/.test(String(c[0])))).toBe(true);
  });

  it('T4/T5: a rejected thread message is NOT readable through its direct id (same generic denial)', async () => {
    const fetchMock = makeThreadFetch();
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, threadCtx());
    for (const hidden of ['INEVIL', 'SPOOF', 'FORGE']) {
      const r = await s.tools.outlook_get_message.handler({ message_id: hidden });
      expect(r.isError).toBe(true);
      expect(r.content[0].text).toBe('Error: message is not available for this task.');
    }
  });

  it('T8: a foreign opaque id is denied WITHOUT a Graph GET of that id (no leak of its conversation)', async () => {
    const fetchMock = makeThreadFetch();
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, threadCtx());
    const r = await s.tools.outlook_get_message.handler({ message_id: 'FOREIGN' });
    expect(r.isError).toBe(true);
    expect(r.content[0].text).toBe('Error: message is not available for this task.');
    expect(fetchMock.mock.calls.every((c) => !String(c[0]).includes('FOREIGN'))).toBe(true);
  });

  it('T9: outlook_reply on an earlier thread message is denied (action scope not widened)', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, threadCtx());
    const r = await s.tools.outlook_reply.handler({ message_id: 'IN1', body: 'hi' });
    expect(r.isError).toBe(true);
    expect(r.content[0].text).toBe('Error: message is not available for this task.');
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('T10: outlook_mark_processed on an earlier thread message is denied', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, threadCtx());
    const r = await s.tools.outlook_mark_processed.handler({ message_id: 'OUT1' });
    expect(r.isError).toBe(true);
    expect(r.content[0].text).toBe('Error: message is not available for this task.');
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('T13: trigger message has no conversationId → fail closed (list_thread errors, thread ids denied)', async () => {
    vi.stubGlobal('fetch', makeThreadFetch({ conversationId: '' }));
    const s = makeServer();
    register(s, threadCtx());
    const r = await s.tools.outlook_list_thread.handler({});
    expect(r.isError).toBe(true);
    expect(r.content[0].text).toBe('Error: thread is not available for this task.');
    const r2 = await s.tools.outlook_get_message.handler({ message_id: 'IN1' });
    expect(r2.isError).toBe(true);
  });

  it('with the feature OFF, an earlier thread id is still denied (read scope unchanged)', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, threadCtx({ allow_thread_read: false }));
    const r = await s.tools.outlook_get_message.handler({ message_id: 'IN1' });
    expect(r.isError).toBe(true);
    expect(r.content[0].text).toBe('Error: message is not available for this task.');
    expect(fetchMock).not.toHaveBeenCalled(); // denied before any Graph call, no thread enumeration
  });

  it('P2: a thread message whose list row OMITS internetMessageHeaders is hydrated, not silently dropped', async () => {
    // Graph collection responses sometimes drop internetMessageHeaders even when $select asks for it. The
    // per-message header response passes DMARC, so the allow-listed message must be surfaced — exactly the
    // hydration fallback the delta poller uses.
    const listNoHeaders = [
      { id: 'IN2', conversationId: 'CONV1', subject: 'Audyt', from: { emailAddress: { address: 'sklep@mycompanystudio.com' } }, receivedDateTime: '2026-09-15T11:07:00Z', hasAttachments: false, parentFolderId: 'INBOX' },
    ];
    const fetchMock = vi.fn(async (url) => {
      const u = String(url);
      if (/\/messages\/TRIG\?\$select=id,conversationId/.test(u)) return jsonRes({ id: 'TRIG', conversationId: 'CONV1' });
      if (/\/mailFolders\/sentitems/.test(u)) return jsonRes({ id: 'SENT' });
      if (/\/messages\?\$filter=/.test(u)) return jsonRes({ value: listNoHeaders });
      if (/\/messages\/IN2\?\$select=internetMessageHeaders/.test(u)) return jsonRes({ internetMessageHeaders: PASS_DMARC });
      return jsonRes({});
    });
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, threadCtx());
    const out = JSON.parse((await s.tools.outlook_list_thread.handler({})).content[0].text);
    expect(out.messages.map((m) => m.message_id)).toEqual(['IN2']); // surfaced, not dropped
    expect(out.incomplete).toBe(false);
    expect(fetchMock.mock.calls.some((c) => /\/messages\/IN2\?\$select=internetMessageHeaders/.test(String(c[0])))).toBe(true);
  });

  it('P2: a header hydration failure fails closed for that message and marks the result incomplete', async () => {
    const listNoHeaders = [
      { id: 'IN2', conversationId: 'CONV1', subject: 'Audyt', from: { emailAddress: { address: 'sklep@mycompanystudio.com' } }, receivedDateTime: '2026-09-15T11:07:00Z', hasAttachments: false, parentFolderId: 'INBOX' },
    ];
    const fetchMock = vi.fn(async (url) => {
      const u = String(url);
      if (/\/messages\/TRIG\?\$select=id,conversationId/.test(u)) return jsonRes({ id: 'TRIG', conversationId: 'CONV1' });
      if (/\/mailFolders\/sentitems/.test(u)) return jsonRes({ id: 'SENT' });
      if (/\/messages\?\$filter=/.test(u)) return jsonRes({ value: listNoHeaders });
      if (/\/messages\/IN2\?\$select=internetMessageHeaders/.test(u)) return { ok: false, status: 503, text: () => Promise.resolve('Service Unavailable') };
      return jsonRes({});
    });
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, threadCtx());
    const out = JSON.parse((await s.tools.outlook_list_thread.handler({})).content[0].text);
    expect(out.messages).toEqual([]); // fail closed — not surfaced without a verifiable DMARC pass
    expect(out.incomplete).toBe(true);
  });

  it('P3: a transient attachment-metadata error is reported incomplete and NOT cached (a retry recovers)', async () => {
    // OUT1 is trusted-outbound (physically in Sent Items) so it is authorized; only its attachment
    // metadata GET fails the first time. That failure must not freeze into the session cache as an empty
    // list — a second call, after Graph recovers, returns the real metadata.
    let attempt = 0;
    const list = [
      { id: 'OUT1', conversationId: 'CONV1', subject: 'Re: Audyt', from: { emailAddress: { address: 'agent@example.com' } }, receivedDateTime: '2026-09-15T11:19:00Z', hasAttachments: true, parentFolderId: 'SENT', internetMessageHeaders: [] },
    ];
    const fetchMock = vi.fn(async (url) => {
      const u = String(url);
      if (/\/messages\/TRIG\?\$select=id,conversationId/.test(u)) return jsonRes({ id: 'TRIG', conversationId: 'CONV1' });
      if (/\/mailFolders\/sentitems/.test(u)) return jsonRes({ id: 'SENT' });
      if (/\/messages\?\$filter=/.test(u)) return jsonRes({ value: list });
      if (/\/messages\/OUT1\/attachments\?\$select=id,name/.test(u)) {
        attempt += 1;
        if (attempt === 1) return { ok: false, status: 503, text: () => Promise.resolve('Service Unavailable') };
        return jsonRes({ value: [{ id: 'ATT1', name: 'r.csv', contentType: 'text/csv', size: 3 }] });
      }
      return jsonRes({});
    });
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, threadCtx());
    const out1 = JSON.parse((await s.tools.outlook_list_thread.handler({})).content[0].text);
    expect(out1.incomplete).toBe(true);
    expect(out1.messages[0].attachments).toEqual([]);
    expect(out1.messages[0].attachments_incomplete).toBe(true);
    // Not cached: the second call re-enumerates and now gets the metadata Graph withheld before.
    const out2 = JSON.parse((await s.tools.outlook_list_thread.handler({})).content[0].text);
    expect(out2.incomplete).toBe(false);
    expect(out2.messages[0].attachments).toEqual([{ attachment_id: 'ATT1', name: 'r.csv', content_type: 'text/csv', size: 3 }]);
    expect(out2.messages[0].attachments_incomplete).toBeUndefined();
  });
});

describe('a getToken rejection surfaced through a tool stays sanitized', () => {
  it('outlook_get_message returns isError without raw credential detail (and makes no Graph call)', async () => {
    const auth = () => ({
      isConfigured: () => true,
      getToken: async () => { throw new Error('Graph token acquisition failed (AuthError)'); },
      getMailboxUserId: () => 'agent@example.com',
    });
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, ctx({ graphAuthFactory: auth }));
    const r = await s.tools.outlook_get_message.handler({ message_id: 'm1' });
    expect(r.isError).toBe(true);
    expect(r.content[0].text).not.toMatch(/client secret|AADSTS|private key/i);
    expect(fetchMock).not.toHaveBeenCalled(); // token acquisition failed before any Graph HTTP call
  });
});
