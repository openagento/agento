import { describe, it, expect, vi, beforeEach } from 'vitest';

import {
  createDeltaHandler, parseDmarcVerdict, isAutoReply, parseAllowedSenders, deriveMailboxAllowedSenders,
} from '../../modules/outlook/toolbox/api-handlers.js';
import { createRequireCapability } from '../capability.js';
// The real shared matcher, injected the way the framework injects it (config-loader
// TOOLBOX_HELPERS) — a module cannot import framework code by path.
import { matchesWhitelist } from '../email-match.js';

// Inject a fake auth so token acquisition needs no real @azure/identity; the only global fetch the
// handler makes is the Graph messages call. isConfigured mirrors graph-auth's real rule.
const fakeAuthFactory = (cfg) => ({
  isConfigured: () => !!(cfg.outlook_tenant_id && cfg.outlook_client_id && cfg.outlook_mailbox_user_id && (cfg.outlook_cert_pem || cfg.outlook_client_secret)),
  getToken: async () => 'AAA',
  getMailboxUserId: () => cfg.outlook_mailbox_user_id,
});

function mockRes() {
  return {
    statusCode: 200,
    body: null,
    status(c) { this.statusCode = c; return this; },
    json(b) { this.body = b; return this; },
  };
}

const cfg = {
  outlook_tenant_id: 'tid', outlook_client_id: 'cid',
  outlook_client_secret: 'sec', outlook_mailbox_user_id: 'agent@example.com',
};

describe('parseDmarcVerdict (first Authentication-Results header wins — anti-spoof)', () => {
  it('returns "pass" for a passing first header', () => {
    expect(parseDmarcVerdict([
      { name: 'Authentication-Results', value: 'spf=pass; dkim=pass; dmarc=pass action=none header.from=mycompanystudio.com' },
    ])).toBe('pass');
  });

  it('returns "fail" for a failing header', () => {
    expect(parseDmarcVerdict([{ name: 'Authentication-Results', value: 'dmarc=fail action=oreject' }])).toBe('fail');
  });

  it('is case-insensitive on header name and verdict', () => {
    expect(parseDmarcVerdict([{ name: 'authentication-results', value: 'DMARC=Pass' }])).toBe('pass');
  });

  it('returns null when there is no Authentication-Results header / no dmarc token', () => {
    expect(parseDmarcVerdict([{ name: 'Received', value: 'from mail.x.com' }])).toBeNull();
    expect(parseDmarcVerdict([])).toBeNull();
    expect(parseDmarcVerdict(undefined)).toBeNull();
    expect(parseDmarcVerdict([{ name: 'Authentication-Results', value: 'spf=pass; dkim=pass' }])).toBeNull();
  });

  it('ANTI-SPOOF: a later injected dmarc=pass header does NOT override a failing first header', () => {
    expect(parseDmarcVerdict([
      { name: 'Authentication-Results', value: 'dmarc=fail' },        // EOP-stamped, trusted, FIRST
      { name: 'Authentication-Results', value: 'dmarc=pass' },        // attacker-injected, lower, ignored
    ])).toBe('fail');
  });

  it('ANTI-SPOOF: a "dmarc=pass" substring inside an attacker-influenced field does NOT forge a pass', () => {
    // The real verdict is the token-anchored `dmarc=`; a literal substring elsewhere must be ignored.
    expect(parseDmarcVerdict([{ name: 'Authentication-Results',
      value: 'spf=fail smtp.mailfrom="dmarc=pass"@evil.com; dmarc=fail action=oreject' }])).toBe('fail');
    expect(parseDmarcVerdict([{ name: 'Authentication-Results',
      value: 'smtp.helo=dmarc=pass.attacker.com; spf=fail; dmarc=fail' }])).toBe('fail');
    expect(parseDmarcVerdict([{ name: 'Authentication-Results',
      value: 'x-dmarc=pass; dmarc=fail' }])).toBe('fail');
  });
});

describe('isAutoReply (RFC 3834 Auto-Submitted + common vendor headers)', () => {
  const h = (name, value) => [{ name, value }];

  it('Auto-Submitted: auto-replied / auto-generated → true', () => {
    expect(isAutoReply(h('Auto-Submitted', 'auto-replied'))).toBe(true);
    expect(isAutoReply(h('Auto-Submitted', 'auto-generated'))).toBe(true);
    expect(isAutoReply(h('auto-submitted', 'Auto-Replied'))).toBe(true); // case-insensitive name + value
  });

  it('Auto-Submitted: no → false', () => {
    expect(isAutoReply(h('Auto-Submitted', 'no'))).toBe(false);
    expect(isAutoReply(h('Auto-Submitted', ' No '))).toBe(false); // trimmed + lowered
  });

  it('no Auto-Submitted, X-Autoreply: yes → true; X-Autorespond present → true', () => {
    expect(isAutoReply(h('X-Autoreply', 'yes'))).toBe(true);
    expect(isAutoReply(h('X-Autorespond', 'anything'))).toBe(true);
    expect(isAutoReply(h('X-Autoreply', 'no'))).toBe(false); // only "yes" counts for X-Autoreply
  });

  it('Precedence: auto_reply → true', () => {
    expect(isAutoReply(h('Precedence', 'auto_reply'))).toBe(true);
    expect(isAutoReply(h('Precedence', 'bulk'))).toBe(false);
  });

  it('X-Auto-Response-Suppress alone → false (deliberately NOT a marker)', () => {
    // It means "do not auto-respond TO me" and rides on ordinary system mail — must not drop it.
    expect(isAutoReply(h('X-Auto-Response-Suppress', 'All'))).toBe(false);
  });

  it('fail-open: absent / non-array / no marker → false (never drop mail on missing headers)', () => {
    expect(isAutoReply([])).toBe(false);
    expect(isAutoReply(undefined)).toBe(false);
    expect(isAutoReply(null)).toBe(false);
    expect(isAutoReply(h('Received', 'from mail.x.com'))).toBe(false);
  });
});

const ok = (c) => async () => ({ cfg: c });
// The /api guard verified this before the handler ran; the handler reads the scope from it.
const CAP = { kind: 'internal_rest', agentViewId: 42, jobId: null };
const capReq = { capability: CAP };
// Admit any single-@ address, so tests about paging/mapping are not also tests of the gate.
// `*` never crosses the `@`, so this is 'anything that looks like an address', not '.*'.
const ALLOW_ALL = async () => ['*@*'];

// Replay a queued list of fetch responses (one per Graph call: delta pages, 410s, hydration GETs).
function queueFetch(responses) {
  const calls = [];
  const fn = vi.fn(async (url) => {
    calls.push(url);
    const n = responses.shift();
    return typeof n === 'function' ? n(url) : n;
  });
  vi.stubGlobal('fetch', fn);
  return { fn, calls };
}
const jsonRes = (body, { ok = true, status = 200 } = {}) => ({
  ok,
  status,
  json: () => Promise.resolve(body),
  text: () => Promise.resolve(''),
});
const hdr = (v) => [{ name: 'Authentication-Results', value: v }];
// A valid deltaLink for a mailbox (folder resolved to an opaque id, as Graph returns).
const linkFor = (mbox, tok) =>
  `https://graph.microsoft.com/v1.0/users/${encodeURIComponent(mbox)}/mailFolders/AAMkFolderId/messages/delta?$deltatoken=${tok}`;
const AGENT_DELTA = (tok) => linkFor('agent@example.com', tok);

describe('POST /api/outlook/delta handler', () => {
  beforeEach(() => vi.unstubAllGlobals());

  it('returns 500 when not configured', async () => {
    const handler = createDeltaHandler(ok({}), vi.fn(), fakeAuthFactory, ALLOW_ALL, matchesWhitelist);
    const res = mockRes();
    await handler({ ...capReq, body: {} }, res);
    expect(res.statusCode).toBe(500);
  });

  it('does a base delta (no cursor), maps messages, and returns the next deltaLink', async () => {
    const { calls } = queueFetch([
      jsonRes({
        value: [{ id: 'm1', subject: 'A', from: { emailAddress: { address: 'x@y.com', name: 'X' } },
                  receivedDateTime: '2026-01-01T00:00:00Z', conversationId: 'c1',
                  internetMessageHeaders: hdr('dmarc=pass') }],
        '@odata.deltaLink': AGENT_DELTA('NEW'),
      }),
    ]);
    const handler = createDeltaHandler(ok(cfg), vi.fn(), fakeAuthFactory, ALLOW_ALL, matchesWhitelist);
    const res = mockRes();
    await handler({ ...capReq, body: { top: 10, cursors: {} } }, res);
    expect(res.statusCode).toBe(200);
    expect(res.body.mailbox).toBe('agent@example.com');
    expect(res.body.messages).toHaveLength(1);
    expect(res.body.messages[0]).toMatchObject({ id: 'm1', dmarc: 'pass' });
    expect(res.body.deltaLink).toBe(AGENT_DELTA('NEW'));
    expect(res.body.resynced).toBe(false);
    expect(calls[0]).toContain('/users/agent%40example.com/mailFolders/Inbox/messages/delta'); // toolbox-built base URL
    expect(calls[0]).toContain('$select=');
  });

  it('resumes by applying a VALID stored deltaLink as-is', async () => {
    const { calls } = queueFetch([jsonRes({ value: [], '@odata.deltaLink': AGENT_DELTA('D2') })]);
    const handler = createDeltaHandler(ok(cfg), vi.fn(), fakeAuthFactory, ALLOW_ALL, matchesWhitelist);
    await handler({ ...capReq, body: { cursors: { 'agent@example.com': AGENT_DELTA('PREV') } } }, mockRes());
    expect(calls[0]).toBe(AGENT_DELTA('PREV')); // applied verbatim, per Graph's contract
  });

  // REGRESSION (AG-40): Graph's @odata.deltaLink comes back in the quoted-key folder form
  // (…/mailFolders('Inbox')/messages/delta), NOT the path-segment form baseDeltaUrl() emits. The old
  // validator regex only accepted the path segment, so every stored cursor was rejected → resynced=true
  // full re-enum on every poll, forever. These assert the quoted-key form (raw and %-encoded) is accepted
  // and replayed verbatim.
  it('AG-40: applies a stored deltaLink in Graph\'s quoted-key folder form as-is (no forced re-enum)', async () => {
    const quoted = "https://graph.microsoft.com/v1.0/users/agent@example.com/mailFolders('Inbox')/messages/delta?$deltatoken=Q";
    const { calls } = queueFetch([jsonRes({ value: [], '@odata.deltaLink': AGENT_DELTA('D2') })]);
    const handler = createDeltaHandler(ok(cfg), vi.fn(), fakeAuthFactory, ALLOW_ALL, matchesWhitelist);
    const res = mockRes();
    await handler({ ...capReq, body: { cursors: { 'agent@example.com': quoted } } }, res);
    expect(calls[0]).toBe(quoted); // applied verbatim, not discarded
    expect(res.body.resynced).toBe(false); // the bug's tell: this used to be true on every poll
  });

  it('AG-40: applies a stored deltaLink in the %-encoded quoted-key folder form as-is', async () => {
    const enc = 'https://graph.microsoft.com/v1.0/users/agent@example.com/mailFolders(%27Inbox%27)/messages/delta?$deltatoken=E';
    const { calls } = queueFetch([jsonRes({ value: [], '@odata.deltaLink': AGENT_DELTA('D2') })]);
    const handler = createDeltaHandler(ok(cfg), vi.fn(), fakeAuthFactory, ALLOW_ALL, matchesWhitelist);
    const res = mockRes();
    await handler({ ...capReq, body: { cursors: { 'agent@example.com': enc } } }, res);
    expect(calls[0]).toBe(enc);
    expect(res.body.resynced).toBe(false);
  });

  it('AG-40: a quoted-key cursor for a FOREIGN mailbox is still rejected → full base enum', async () => {
    const foreign = "https://graph.microsoft.com/v1.0/users/victim@example.com/mailFolders('Inbox')/messages/delta?$deltatoken=V";
    const { calls } = queueFetch([jsonRes({ value: [], '@odata.deltaLink': AGENT_DELTA('D') })]);
    const handler = createDeltaHandler(ok(cfg), vi.fn(), fakeAuthFactory, ALLOW_ALL, matchesWhitelist);
    const res = mockRes();
    await handler({ ...capReq, body: { cursors: { 'agent@example.com': foreign } } }, res);
    expect(calls[0]).not.toContain('victim'); // mailbox-equality guard survives the widened folder shape
    expect(calls[0]).toContain('/users/agent%40example.com/mailFolders/Inbox/messages/delta');
    expect(res.body.resynced).toBe(true);
  });

  it('AG-40: a quoted key that smuggles a path separator is rejected → full base enum (no traversal)', async () => {
    // The quoted-key alternatives exclude `/` and the quote char, so an injected extra path segment
    // cannot slip a foreign structure past the shape check.
    const traversal = "https://graph.microsoft.com/v1.0/users/agent@example.com/mailFolders('a')/x/messages/delta?$deltatoken=T";
    const { calls } = queueFetch([jsonRes({ value: [], '@odata.deltaLink': AGENT_DELTA('D') })]);
    const handler = createDeltaHandler(ok(cfg), vi.fn(), fakeAuthFactory, ALLOW_ALL, matchesWhitelist);
    await handler({ ...capReq, body: { cursors: { 'agent@example.com': traversal } } }, mockRes());
    expect(calls[0]).toContain('/users/agent%40example.com/mailFolders/Inbox/messages/delta'); // base URL, cursor discarded
  });

  it('SSRF/cross-mailbox: discards a cursor whose user segment is NOT the resolved mailbox → full base enum', async () => {
    const { calls } = queueFetch([jsonRes({ value: [], '@odata.deltaLink': AGENT_DELTA('D') })]);
    const handler = createDeltaHandler(ok(cfg), vi.fn(), fakeAuthFactory, ALLOW_ALL, matchesWhitelist);
    const res = mockRes();
    // A real Graph deltaLink for ANOTHER mailbox (victim) — would read victim's mail with the app token.
    await handler({ ...capReq, body: { cursors: { 'agent@example.com': linkFor('victim@example.com', 'V') } } }, res);
    expect(calls[0]).toContain('/users/agent%40example.com/'); // resolved mailbox base URL...
    expect(calls[0]).not.toContain('victim'); // ...NOT the foreign cursor
    expect(calls[0]).toContain('$select='); // it's a base enumeration
    expect(res.body.resynced).toBe(true); // signalled as a forced re-enumeration
  });

  it('SSRF: discards a non-graph-host / credentialed / non-delta cursor → full base enum (no token-bearing fetch of it)', async () => {
    for (const bad of [
      'https://attacker.example/v1.0/users/agent@example.com/mailFolders/x/messages/delta?$deltatoken=T',
      'http://graph.microsoft.com/v1.0/users/agent@example.com/mailFolders/x/messages/delta?$deltatoken=T',
      'https://user:p@graph.microsoft.com/v1.0/users/agent@example.com/mailFolders/x/messages/delta?$deltatoken=T',
      'https://graph.microsoft.com/v1.0/users/agent@example.com/messages', // not a delta path
      12345,
    ]) {
      const { calls } = queueFetch([jsonRes({ value: [], '@odata.deltaLink': AGENT_DELTA('D') })]);
      const handler = createDeltaHandler(ok(cfg), vi.fn(), fakeAuthFactory, ALLOW_ALL, matchesWhitelist);
      await handler({ ...capReq, body: { cursors: { 'agent@example.com': bad } } }, mockRes());
      expect(calls[0]).toContain('/users/agent%40example.com/mailFolders/Inbox/messages/delta'); // base URL, never the bad cursor
      vi.unstubAllGlobals();
    }
  });

  it('discards a same-mailbox delta URL that lacks $deltatoken (not a real cursor) → full base enum', async () => {
    const { calls } = queueFetch([jsonRes({ value: [], '@odata.deltaLink': AGENT_DELTA('D') })]);
    const handler = createDeltaHandler(ok(cfg), vi.fn(), fakeAuthFactory, ALLOW_ALL, matchesWhitelist);
    await handler({ ...capReq, body: { cursors: {
      'agent@example.com': 'https://graph.microsoft.com/v1.0/users/agent@example.com/mailFolders/Inbox/messages/delta?changeType=deleted' } } }, mockRes());
    expect(calls[0]).toContain('/users/agent%40example.com/mailFolders/Inbox/messages/delta');
    expect(calls[0]).toContain('$select='); // base enumeration, not the token-less caller URL
    expect(calls[0]).not.toContain('changeType');
  });

  it('does NOT crash on a cursor with malformed %-encoding — discards it → full base enum', async () => {
    const { calls } = queueFetch([jsonRes({ value: [], '@odata.deltaLink': AGENT_DELTA('D') })]);
    const handler = createDeltaHandler(ok(cfg), vi.fn(), fakeAuthFactory, ALLOW_ALL, matchesWhitelist);
    const res = mockRes();
    await handler({ ...capReq, body: { cursors: {
      'agent@example.com': 'https://graph.microsoft.com/v1.0/users/%ZZ/mailFolders/x/messages/delta?$deltatoken=T' } } }, res);
    expect(res.statusCode).toBe(200); // no uncaught throw
    expect(calls[0]).toContain('/users/agent%40example.com/mailFolders/Inbox/messages/delta');
  });

  it('pages @odata.nextLink to the end and concatenates messages', async () => {
    queueFetch([
      jsonRes({ value: [{ id: 'a', from: { emailAddress: { address: 'x@y.com' } }, internetMessageHeaders: hdr('dmarc=pass') }],
                '@odata.nextLink': 'https://graph.microsoft.com/v1.0/page2' }),
      jsonRes({ value: [{ id: 'b', from: { emailAddress: { address: 'x@y.com' } }, internetMessageHeaders: hdr('dmarc=pass') }],
                '@odata.deltaLink': AGENT_DELTA('END') }),
    ]);
    const handler = createDeltaHandler(ok(cfg), vi.fn(), fakeAuthFactory, ALLOW_ALL, matchesWhitelist);
    const res = mockRes();
    await handler({ ...capReq, body: { cursors: { 'agent@example.com': AGENT_DELTA('start') } } }, res);
    expect(res.body.messages.map((m) => m.id)).toEqual(['a', 'b']);
    expect(res.body.deltaLink).toBe(AGENT_DELTA('END'));
  });

  it('FAIL-CLOSED RESYNC: a 410 on a stored cursor restarts a full base-delta enumeration (resynced=true)', async () => {
    const { calls } = queueFetch([
      jsonRes({ error: { code: 'syncStateNotFound' } }, { ok: false, status: 410 }),
      jsonRes({ value: [{ id: 'r1', from: { emailAddress: { address: 'x@y.com' } }, internetMessageHeaders: hdr('dmarc=pass') }],
                '@odata.deltaLink': AGENT_DELTA('FRESH') }),
    ]);
    const handler = createDeltaHandler(ok(cfg), vi.fn(), fakeAuthFactory, ALLOW_ALL, matchesWhitelist);
    const res = mockRes();
    await handler({ ...capReq, body: { cursors: { 'agent@example.com': AGENT_DELTA('STALE') } } }, res);
    expect(res.statusCode).toBe(200);
    expect(res.body.resynced).toBe(true);
    expect(res.body.messages.map((m) => m.id)).toEqual(['r1']);
    expect(res.body.deltaLink).toBe(AGENT_DELTA('FRESH'));
    expect(calls[0]).toBe(AGENT_DELTA('STALE')); // tried the stale cursor first
    expect(calls[1]).toContain('/users/agent%40example.com/mailFolders/Inbox/messages/delta'); // then a fresh base enum
  });

  it('FAIL-CLOSED RESYNC: a 40x carrying error.code syncStateNotFound also restarts a full base enumeration', async () => {
    const synErr = { ok: false, status: 400,
      json: () => Promise.resolve({ error: { code: 'syncStateNotFound' } }),
      text: () => Promise.resolve(JSON.stringify({ error: { code: 'syncStateNotFound' } })) };
    const { calls } = queueFetch([
      synErr,
      jsonRes({ value: [{ id: 'r1', from: { emailAddress: { address: 'x@y.com' } }, internetMessageHeaders: hdr('dmarc=pass') }],
                '@odata.deltaLink': AGENT_DELTA('FRESH') }),
    ]);
    const handler = createDeltaHandler(ok(cfg), vi.fn(), fakeAuthFactory, ALLOW_ALL, matchesWhitelist);
    const res = mockRes();
    await handler({ ...capReq, body: { cursors: { 'agent@example.com': AGENT_DELTA('STALE') } } }, res);
    expect(res.statusCode).toBe(200);
    expect(res.body.resynced).toBe(true);
    expect(res.body.messages.map((m) => m.id)).toEqual(['r1']);
    expect(calls[1]).toContain('/users/agent%40example.com/mailFolders/Inbox/messages/delta');
  });

  it('hydrates internetMessageHeaders via a per-message GET when the delta item omits them', async () => {
    const { calls } = queueFetch([
      jsonRes({ value: [{ id: 'm9', from: { emailAddress: { address: 'x@y.com' } }, receivedDateTime: '2026-01-01T00:00:00Z' }],
                '@odata.deltaLink': AGENT_DELTA('D') }),
      jsonRes({ internetMessageHeaders: hdr('dmarc=pass') }), // hydration GET
    ]);
    const handler = createDeltaHandler(ok(cfg), vi.fn(), fakeAuthFactory, ALLOW_ALL, matchesWhitelist);
    const res = mockRes();
    await handler({ ...capReq, body: { cursors: {} } }, res);
    expect(res.body.messages[0].dmarc).toBe('pass');
    expect(calls[1]).toContain('/messages/m9');
    expect(calls[1]).toContain('internetMessageHeaders');
  });

  it('skips @removed (moved/deleted) delta items — no hydration fetch, 200, deltaLink still returned', async () => {
    const { calls } = queueFetch([
      jsonRes({ value: [
        { id: 'rm1', '@removed': { reason: 'deleted' } },
        { id: 'rm2', '@removed': { reason: 'changed' } },
      ], '@odata.deltaLink': AGENT_DELTA('D') }),
    ]);
    const handler = createDeltaHandler(ok(cfg), vi.fn(), fakeAuthFactory, ALLOW_ALL, matchesWhitelist);
    const res = mockRes();
    await handler({ ...capReq, body: { cursors: {} } }, res);
    expect(res.statusCode).toBe(200);
    expect(res.body.messages).toEqual([]); // removed items are folder-sync events, not publishable mail
    expect(res.body.deltaLink).toBe(AGENT_DELTA('D'));
    expect(calls).toHaveLength(1); // ONLY the delta page — no per-message hydration GET (would 404 → 502 → pin)
  });

  it('FAIL-CLOSED: per-message header hydration failure returns 502 (publisher will hold the cursor)', async () => {
    queueFetch([
      jsonRes({ value: [{ id: 'm9', from: { emailAddress: { address: 'x@y.com' } } }], '@odata.deltaLink': AGENT_DELTA('D') }),
      jsonRes({}, { ok: false, status: 500 }), // hydration GET fails
    ]);
    const handler = createDeltaHandler(ok(cfg), vi.fn(), fakeAuthFactory, ALLOW_ALL, matchesWhitelist);
    const res = mockRes();
    await handler({ ...capReq, body: { cursors: {} } }, res);
    expect(res.statusCode).toBe(502);
  });

  it('FAIL-CLOSED: returns 502 if paging never reaches an @odata.deltaLink (no partial success)', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
      jsonRes({ value: [], '@odata.nextLink': 'https://graph.microsoft.com/v1.0/next' }))); // only ever nextLink → cap hit
    const handler = createDeltaHandler(ok(cfg), vi.fn(), fakeAuthFactory, ALLOW_ALL, matchesWhitelist);
    const res = mockRes();
    await handler({ ...capReq, body: { cursors: {} } }, res);
    expect(res.statusCode).toBe(502);
  });

  it('derives agent_view_id from the capability and ignores a body value it agrees with', async () => {
    queueFetch([jsonRes({ value: [], '@odata.deltaLink': AGENT_DELTA('D') }),
                jsonRes({ value: [], '@odata.deltaLink': AGENT_DELTA('D') })]);
    const resolver = vi.fn(async () => ({ cfg }));
    const handler = createDeltaHandler(resolver, vi.fn(), fakeAuthFactory, ALLOW_ALL, matchesWhitelist);
    await handler({ ...capReq, body: { agent_view_id: 42, cursors: {} } }, mockRes());
    await handler({ ...capReq, body: { cursors: {} } }, mockRes());
    expect(resolver).toHaveBeenNthCalledWith(1, 42);
    expect(resolver).toHaveBeenNthCalledWith(2, 42); // NOT null — the body never selects the scope
  });

  // The conflicting-claim rejection moved OUT of this handler: createModuleRouteApp applies it to
  // every module route, so no module can forget it. Its test lives in rest-auth-coverage.test.js.
});

// The route chain is guard -> handler. These compose the real middleware with the real handler,
// so they prove the delta route itself is unreachable without an internal_rest capability —
// not merely that the middleware, in isolation, rejects things.
describe('delta route capability enforcement (guard + handler)', () => {
  beforeEach(() => vi.unstubAllGlobals());

  // Runs the chain the way Express would: middleware first, handler only if next() is called.
  async function callRoute(verify, req) {
    const res = mockRes();
    const mw = createRequireCapability(verify, { kinds: ['internal_rest'] }, () => {});
    const handler = createDeltaHandler(ok(cfg), vi.fn(), fakeAuthFactory, ALLOW_ALL, matchesWhitelist);
    await new Promise((resolve) => {
      const next = () => handler(req, res).then(resolve, resolve);
      Promise.resolve(mw(req, res, next)).then(() => resolve(), () => resolve());
    });
    return res;
  }

  it('401s with no capability and makes zero Graph calls', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const res = await callRoute(async () => null, { headers: {}, body: {} });
    expect(res.statusCode).toBe(401);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('403s for a forged or expired capability and makes zero Graph calls', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    // An unknown or expired token is indistinguishable to the verifier: both return null.
    const res = await callRoute(async () => null, { headers: { authorization: 'Bearer forged' }, body: {} });
    expect(res.statusCode).toBe(403);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('403s for an mcp_job capability — an agent cannot start a mailbox sync', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    // The real verifier returns null when the row's kind is outside the guard's `kinds`.
    const verify = async (t, { kinds }) => (kinds.includes('mcp_job') ? { kind: 'mcp_job', agentViewId: 42, jobId: '9' } : null);
    const res = await callRoute(verify, { headers: { authorization: 'Bearer agent-held' }, body: {} });
    expect(res.statusCode).toBe(403);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('preserves cursor and resync behaviour for a valid capability', async () => {
    queueFetch([jsonRes({ value: [], '@odata.deltaLink': AGENT_DELTA('NEXT') })]);
    const res = await callRoute(async () => ({ ...CAP }),
      { headers: { authorization: 'Bearer good' },
        body: { cursors: { 'agent@example.com': AGENT_DELTA('PREV') } } });
    expect(res.statusCode).toBe(200);
    expect(res.body.resynced).toBe(false);
    expect(res.body.deltaLink).toBe(AGENT_DELTA('NEXT'));
  });
});

describe('delta map carries to / cc / bodyPreview (activation plumbing)', () => {
  beforeEach(() => vi.unstubAllGlobals());

  it('maps toRecipients, ccRecipients and bodyPreview, and $selects them', async () => {
    const { calls } = queueFetch([
      jsonRes({
        value: [{
          id: 'm1', subject: 'A',
          from: { emailAddress: { address: 'x@y.com', name: 'X' } },
          toRecipients: [{ emailAddress: { address: 'to1@y.com', name: 'To1' } }],
          ccRecipients: [{ emailAddress: { address: 'cc1@y.com', name: 'Cc1' } }],
          bodyPreview: 'hello there',
          receivedDateTime: '2026-01-01T00:00:00Z', conversationId: 'c1',
          internetMessageHeaders: hdr('dmarc=pass'),
        }],
        '@odata.deltaLink': AGENT_DELTA('NEW'),
      }),
    ]);
    const handler = createDeltaHandler(ok(cfg), vi.fn(), fakeAuthFactory, ALLOW_ALL, matchesWhitelist);
    const res = mockRes();
    await handler({ ...capReq, body: { cursors: {} } }, res);
    const m = res.body.messages[0];
    expect(m.to).toEqual([{ name: 'To1', address: 'to1@y.com' }]);
    expect(m.cc).toEqual([{ name: 'Cc1', address: 'cc1@y.com' }]);
    expect(m.bodyPreview).toBe('hello there');
    // the base delta URL selects the new fields
    expect(calls[0]).toContain('toRecipients');
    expect(calls[0]).toContain('ccRecipients');
    expect(calls[0]).toContain('bodyPreview');
  });

  it('defaults to/cc to [] when the item omits them', async () => {
    queueFetch([
      jsonRes({
        value: [{ id: 'm1', from: { emailAddress: { address: 'x@y.com' } }, internetMessageHeaders: hdr('dmarc=pass') }],
        '@odata.deltaLink': AGENT_DELTA('NEW'),
      }),
    ]);
    const handler = createDeltaHandler(ok(cfg), vi.fn(), fakeAuthFactory, ALLOW_ALL, matchesWhitelist);
    const res = mockRes();
    await handler({ ...capReq, body: { cursors: {} } }, res);
    expect(res.body.messages[0].to).toEqual([]);
    expect(res.body.messages[0].cc).toEqual([]);
  });
});

describe('agent_authored (fleet-mailbox loop detection) in the delta map', () => {
  beforeEach(() => vi.unstubAllGlobals());

  // Loop suppression uses fleet-address detection (no header/HMAC): agent_authored iff the message From
  // is in the resolver-supplied fleetMailboxes set (auto-derived from the agent_views, not hand-listed).
  // The DMARC gate is applied later by the publisher, not here.
  const runWith = async (fleetMailboxes, fromAddress) => {
    queueFetch([
      jsonRes({
        value: [{ id: 'm1', from: { emailAddress: { address: fromAddress } }, internetMessageHeaders: hdr('dmarc=pass') }],
        '@odata.deltaLink': AGENT_DELTA('D'),
      }),
    ]);
    const resolver = async () => ({ cfg, fleetMailboxes });
    const handler = createDeltaHandler(resolver, vi.fn(), fakeAuthFactory, ALLOW_ALL, matchesWhitelist);
    const res = mockRes();
    await handler({ ...capReq, body: { cursors: {} } }, res);
    return res;
  };

  it('From in the fleet set → agent_authored true (case-insensitive)', async () => {
    const res = await runWith(new Set(['peer-bot@example.com']), 'Peer-Bot@Example.com');
    expect(res.body.messages[0].agent_authored).toBe(true);
  });

  it('From not in the fleet set → agent_authored false', async () => {
    const res = await runWith(new Set(['peer-bot@example.com']), 'human@example.com');
    expect(res.body.messages[0].agent_authored).toBe(false);
  });

  it('empty fleet set (no peers) → agent_authored false', async () => {
    const res = await runWith(new Set(), 'peer-bot@example.com');
    expect(res.body.messages[0].agent_authored).toBe(false);
  });

  it('resolver omits fleetMailboxes entirely → treated as empty (agent_authored false)', async () => {
    const res = await runWith(undefined, 'peer-bot@example.com');
    expect(res.body.messages[0].agent_authored).toBe(false);
  });
});

describe('auto_reply (RFC 3834 drop signal) in the delta map', () => {
  beforeEach(() => vi.unstubAllGlobals());

  const runWith = async (headerValue) => {
    queueFetch([
      jsonRes({
        value: [{
          id: 'm1', from: { emailAddress: { address: 'oof@example.com' } },
          internetMessageHeaders: [
            { name: 'Authentication-Results', value: 'dmarc=pass' },
            { name: 'Auto-Submitted', value: headerValue },
          ],
        }],
        '@odata.deltaLink': AGENT_DELTA('D'),
      }),
    ]);
    const handler = createDeltaHandler(ok(cfg), vi.fn(), fakeAuthFactory, ALLOW_ALL, matchesWhitelist);
    const res = mockRes();
    await handler({ ...capReq, body: { cursors: {} } }, res);
    return res;
  };

  it('an out-of-office auto-reply carries auto_reply: true (alongside its dmarc verdict)', async () => {
    const res = await runWith('auto-replied');
    expect(res.body.messages[0]).toMatchObject({ dmarc: 'pass', auto_reply: true });
  });

  it('an ordinary human reply carries auto_reply: false', async () => {
    const res = await runWith('no');
    expect(res.body.messages[0].auto_reply).toBe(false);
  });
});

// The inbound gate used to live in Python, AFTER the toolbox had already returned every message.
// A blocked sender's id, subject, recipients and preview reached the agent's job payload before
// anything rejected them. Gating inside the handler means they never leave the toolbox at all.
describe('inbound gate inside the delta handler', () => {
  beforeEach(() => vi.unstubAllGlobals());

  const msg = (id, address, dmarc = 'dmarc=pass') => ({
    id, subject: `subject of ${id}`,
    from: { emailAddress: { address, name: 'Sender' } },
    toRecipients: [{ emailAddress: { address: 'agent@example.com' } }],
    ccRecipients: [{ emailAddress: { address: 'watcher@example.com' } }],
    bodyPreview: `preview of ${id}`,
    receivedDateTime: '2026-01-01T00:00:00Z', conversationId: `c-${id}`,
    internetMessageHeaders: hdr(dmarc),
  });

  async function runDelta(items, allowed) {
    queueFetch([jsonRes({ value: items, '@odata.deltaLink': AGENT_DELTA('NEXT') })]);
    const handler = createDeltaHandler(ok(cfg), vi.fn(), fakeAuthFactory, async () => allowed, matchesWhitelist);
    const res = mockRes();
    await handler({ ...capReq, body: { cursors: {} } }, res);
    return res;
  }

  it('drops a non-allow-listed sender entirely — no id, subject, recipients or preview', async () => {
    const res = await runDelta([msg('m1', 'stranger@evil.com')], ['*@corp.com']);
    expect(res.body.messages).toEqual([]);
    // Nothing about the message may survive anywhere in the response.
    expect(JSON.stringify(res.body)).not.toContain('m1');
    expect(JSON.stringify(res.body)).not.toContain('subject of m1');
    expect(JSON.stringify(res.body)).not.toContain('preview of m1');
    expect(JSON.stringify(res.body)).not.toContain('watcher@example.com');
  });

  it('drops a DMARC-failing message from an allow-listed sender', async () => {
    const res = await runDelta([msg('m1', 'bob@corp.com', 'dmarc=fail')], ['*@corp.com']);
    expect(res.body.messages).toEqual([]);
  });

  it('returns id, subject, to/cc and preview only for admitted messages', async () => {
    const res = await runDelta(
      [msg('good', 'bob@corp.com'), msg('bad', 'stranger@evil.com')], ['*@corp.com']);
    expect(res.body.messages).toHaveLength(1);
    expect(res.body.messages[0].id).toBe('good');
    expect(res.body.messages[0].subject).toBe('subject of good');
    expect(res.body.messages[0].bodyPreview).toBe('preview of good');
    expect(res.body.messages[0].cc[0].address).toBe('watcher@example.com');
  });

  it('hydrates DMARC headers ONLY for allow-listed senders', async () => {
    const bare = (id, address) => { const m = msg(id, address); delete m.internetMessageHeaders; return m; };
    const { calls } = queueFetch([
      jsonRes({ value: [bare('bad', 'stranger@evil.com'), bare('good', 'bob@corp.com')],
                '@odata.deltaLink': AGENT_DELTA('NEXT') }),
      jsonRes({ internetMessageHeaders: hdr('dmarc=pass') }),
    ]);
    const handler = createDeltaHandler(ok(cfg), vi.fn(), fakeAuthFactory, async () => ['*@corp.com'], matchesWhitelist);
    const res = mockRes();
    await handler({ ...capReq, body: { cursors: {} } }, res);
    expect(res.body.messages).toHaveLength(1);
    // One delta page + exactly one hydration GET — the blocked message cost no Graph call.
    expect(calls).toHaveLength(2);
    expect(calls[1]).toContain('/messages/good?');
  });

  it('advances the cursor even when every message is dropped', async () => {
    const res = await runDelta([msg('m1', 'stranger@evil.com')], ['*@corp.com']);
    expect(res.statusCode).toBe(200);
    expect(res.body.deltaLink).toBe(AGENT_DELTA('NEXT'));
    expect(res.body.resynced).toBe(false);
  });

  it('blocks everything when the union is empty', async () => {
    const res = await runDelta([msg('m1', 'bob@corp.com')], []);
    expect(res.body.messages).toEqual([]);
  });

  it('does not let a wildcard cross the @ — *@corp.com rejects a@b@corp.com', async () => {
    const res = await runDelta([msg('m1', 'a@b@corp.com')], ['*@corp.com']);
    expect(res.body.messages).toEqual([]);
  });

  it('a handler wired with no union callback admits nothing (fail-closed default)', async () => {
    queueFetch([jsonRes({ value: [msg('m1', 'bob@corp.com')], '@odata.deltaLink': AGENT_DELTA('NEXT') })]);
    const handler = createDeltaHandler(ok(cfg), vi.fn(), fakeAuthFactory);
    const res = mockRes();
    await handler({ ...capReq, body: { cursors: {} } }, res);
    expect(res.body.messages).toEqual([]);
  });
});

describe('deriveMailboxAllowedSenders (the union across views sharing a mailbox)', () => {
  const view = (mailbox, allowed, enabled = 'true') => ({
    enabled, outlook_mailbox_user_id: mailbox, allowed_senders: allowed,
  });

  const derive = (views, mailbox, log = () => {}) =>
    deriveMailboxAllowedSenders({
      listActiveAgentViewIds: async () => Object.keys(views).map(Number),
      resolveOutlookConfig: async (id) => views[id],
      mailbox,
    }, log);

  it('parses a comma-separated list, trimming and lowercasing', () => {
    expect(parseAllowedSenders(' A@x.com , *@Y.com ,, ')).toEqual(['a@x.com', '*@y.com']);
    expect(parseAllowedSenders(undefined)).toEqual([]);
  });

  it('unions allowed_senders across every view sharing the mailbox', async () => {
    const views = {
      1: view('shared@example.com', 'a@x.com'),
      2: view('shared@example.com', 'b@y.com,a@x.com'),
    };
    expect((await derive(views, 'shared@example.com')).sort()).toEqual(['a@x.com', 'b@y.com']);
  });

  it('excludes views bound to a different mailbox from the union', async () => {
    const views = {
      1: view('shared@example.com', 'a@x.com'),
      2: view('other@example.com', 'elsewhere@z.com'),
    };
    expect(await derive(views, 'shared@example.com')).toEqual(['a@x.com']);
  });

  it('excludes a view whose outlook module is disabled', async () => {
    const views = {
      1: view('shared@example.com', 'a@x.com'),
      2: view('shared@example.com', 'disabled@z.com', 'false'),
    };
    expect(await derive(views, 'shared@example.com')).toEqual(['a@x.com']);
  });

  it('matches the mailbox case-insensitively and ignores surrounding whitespace', async () => {
    const views = { 1: view('  Shared@Example.com ', 'a@x.com') };
    expect(await derive(views, 'shared@example.com')).toEqual(['a@x.com']);
  });

  it('FAIL-CLOSED: an unresolvable union admits nothing and is logged', async () => {
    const lines = [];
    const result = await deriveMailboxAllowedSenders({
      listActiveAgentViewIds: async () => { throw new Error('db down'); },
      resolveOutlookConfig: async () => ({}),
      mailbox: 'shared@example.com',
      errorCategory: (err) => err?.code || err?.name || 'Error',
    }, (...a) => lines.push(a.join(' ')));
    expect(result).toEqual([]);
    expect(lines.join(' ')).toContain('allowed-sender union failed');
  });
});
