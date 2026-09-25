import { describe, it, expect, vi } from 'vitest';
import express from 'express';
import net from 'node:net';
import { Readable } from 'node:stream';
import { gzipSync } from 'node:zlib';

// fetch refuses to send a malformed Content-Length, so this one goes over a raw socket.
function rawPost(url, headers) {
  const { hostname, port, pathname } = new URL(url);
  return new Promise((resolve, reject) => {
    const sock = net.connect(Number(port), hostname, () => {
      sock.write(`POST ${pathname} HTTP/1.1\r\nHost: ${hostname}\r\nConnection: close\r\n${headers}\r\n`);
    });
    let out = '';
    sock.on('data', d => { out += d; });
    sock.on('end', () => resolve(out));
    sock.on('error', reject);
  });
}
import { z } from 'zod';
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js';
import {
  executeTool, installToolDispatch, argsSha256, createAuditStore, createConsumer,
  CONSUME_CAPABILITY_SQL, HTTP_STATUS,
} from '../dispatcher.js';
import { installInvokeRoute } from '../invoke-route.js';
import { ScopeUnavailableError } from '../config-loader.js';
import { capabilityContext } from './capability-rows.js';

const SECRET = 'raw-capability-value';

function auditStore() {
  const rows = new Map();
  return {
    rows,
    insert: vi.fn(async (row) => { rows.set(row.execution_id, { ...row, outcome: 'pending' }); }),
    finalize: vi.fn(async (id, outcome) => { rows.get(id).outcome = outcome; }),
  };
}

// An atomic conditional update, like the real CONSUME_CAPABILITY_SQL: one winner per id.
function consumer() {
  const used = new Set();
  return vi.fn(async (id) => {
    await Promise.resolve();
    if (used.has(id)) return false;
    used.add(id);
    return true;
  });
}

function registryWith(tools, { enabled = () => true, unavailable = [] } = {}) {
  return {
    toolNames: Object.keys(tools),
    tools: new Map(Object.entries(tools)),
    unavailableTools: new Set(unavailable),
    isEnabled: (name, overrides) => enabled(name, overrides),
  };
}

const echo = {
  schema: { text: z.string() },
  handler: vi.fn(async ({ text }) => ({ content: [{ type: 'text', text }] })),
};

const USER = {
  ...capabilityContext('mcp_job'), kind: 'user_session', actor: 'user', subject_id: 'u-9',
  agent_view_id: null, workspace_id: 3, job_id: null, capability_id: '201',
};

function deps(overrides = {}) {
  const context = overrides.context || capabilityContext('mcp_job');
  return {
    endpoint: 'mcp',
    audit: auditStore(),
    consume: consumer(),
    reverify: vi.fn(async () => ({ context, single_use: false, permitted_tools: null })),
    loadRegistry: async () => registryWith({ echo }),
    loadOverrides: vi.fn(async () => ({})),
    log: vi.fn(),
    ...overrides,
  };
}

async function run(context, name, args, d) {
  const response = await executeTool(context, name, args, d);
  const row = d.audit.rows.get(response.execution_id);
  return { response, row };
}

describe('executeTool', () => {
  it('runs the tool and audits who, what (digest only), scope and outcome', async () => {
    const d = deps();
    const { response, row } = await run(capabilityContext('mcp_job'), 'echo', { text: 'hi' }, d);
    expect(response).toMatchObject({ ok: true, result: { content: [{ type: 'text', text: 'hi' }] } });
    expect(response.execution_id).toMatch(/^[0-9a-f-]{36}$/);
    expect(row).toMatchObject({
      outcome: 'ok', capability_id: '1', transport: 'http', actor: 'agent', subject_id: '7',
      on_behalf_of: null, tool_name: 'echo', agent_view_id: 7, workspace_id: 3,
      args_sha256: argsSha256({ text: 'hi' }),
    });
    expect(JSON.stringify(row)).not.toContain('hi"');
  });

  it('writes the audit row before anything else, and runs nothing when that write fails', async () => {
    const d = deps({ audit: { insert: vi.fn(async () => { throw new Error('db down'); }), finalize: vi.fn() } });
    const reg = vi.fn();
    const response = await executeTool(capabilityContext('mcp_job'), 'echo', { text: 'x' },
      { ...d, loadRegistry: reg });
    expect(response.error.code).toBe('unavailable');
    expect(reg).not.toHaveBeenCalled();
    expect(d.reverify).not.toHaveBeenCalled();
  });

  it('keeps the result when the finalize fails — the row stays pending', async () => {
    const d = deps();
    d.audit.finalize = vi.fn(async () => { throw new Error('db down'); });
    const { response, row } = await run(capabilityContext('mcp_job'), 'echo', { text: 'x' }, d);
    expect(response.ok).toBe(true);
    expect(row.outcome).toBe('pending');
    expect(d.log).toHaveBeenCalledWith('audit', 'ERROR', expect.stringContaining('finalize'));
  });

  it('re-verifies the capability and its source on every call', async () => {
    const d = deps();
    const ctx = capabilityContext('mcp_job');
    expect((await run(ctx, 'echo', { text: 'a' }, d)).response.ok).toBe(true);
    d.reverify.mockResolvedValueOnce(null);   // source revoked between two calls
    const { response, row } = await run(ctx, 'echo', { text: 'b' }, d);
    expect(response.error.code).toBe('unauthorized');
    expect(row.outcome).toBe('unauthorized');
    expect(d.reverify).toHaveBeenCalledWith('1', { endpoint: 'mcp' });
  });

  it('refuses a disabled tool at its next call as not_found', async () => {
    let enabled = true;
    const d = deps({ loadRegistry: async () => registryWith({ echo }, { enabled: () => enabled }) });
    const ctx = capabilityContext('mcp_job');
    expect((await run(ctx, 'echo', { text: 'a' }, d)).response.ok).toBe(true);
    enabled = false;
    expect((await run(ctx, 'echo', { text: 'b' }, d)).response.error.code).toBe('not_found');
    expect(d.loadOverrides).toHaveBeenCalledTimes(2);
  });

  it('maps unknown and unavailable tools, and a registry failure', async () => {
    const d = deps({ loadRegistry: async () => registryWith({ echo }, { unavailable: ['broken'] }) });
    const ctx = capabilityContext('mcp_job');
    expect((await run(ctx, 'nope', {}, d)).response.error.code).toBe('not_found');
    expect((await run(ctx, 'broken', {}, d)).response.error.code).toBe('unavailable');
    const half = deps({ loadRegistry: async () => registryWith({ echo }, { unavailable: ['echo'] }) });
    expect((await run(ctx, 'echo', { text: 'a' }, half)).response.error.code).toBe('unavailable');
    const failing = deps({ loadRegistry: async () => { throw new Error('import failed'); } });
    const { response, row } = await run(ctx, 'echo', {}, failing);
    expect(response.error.code).toBe('unavailable');
    expect(row.outcome).toBe('unavailable');
  });

  it('a scope that cannot be read is unavailable, never a fallback', async () => {
    const d = deps({ loadOverrides: async () => { throw new ScopeUnavailableError('down'); } });
    expect((await run(capabilityContext('mcp_job'), 'echo', { text: 'a' }, d)).response.error.code)
      .toBe('unavailable');
  });

  it('narrows by tool_ceiling and by the source permitted_tools', async () => {
    const other = { schema: {}, handler: vi.fn(async () => ({ content: [] })) };
    const loadRegistry = async () => registryWith({ echo, other });
    const ceilinged = { ...capabilityContext('mcp_job'), tool_ceiling: ['echo'] };
    const d1 = deps({ loadRegistry, context: ceilinged });
    expect((await run(ceilinged, 'other', {}, d1)).response.error.code).toBe('unauthorized');
    expect((await run(ceilinged, 'echo', { text: 'a' }, d1)).response.ok).toBe(true);

    const d2 = deps({ loadRegistry });
    d2.reverify = vi.fn(async () => ({ context: USER, single_use: true, permitted_tools: ['echo'] }));
    expect((await run(USER, 'other', {}, d2)).response.error.code).toBe('unauthorized');
    expect(other.handler).not.toHaveBeenCalled();
  });

  it('validates arguments strictly — a missing, mistyped or extra field is invalid_arguments', async () => {
    const d = deps();
    const ctx = capabilityContext('mcp_job');
    for (const args of [{}, { text: 1 }, { text: 'a', extra: true }, 'text', null]) {
      const { response, row } = await run(ctx, 'echo', args, d);
      expect(response.error.code).toBe('invalid_arguments');
      expect(row.outcome).toBe('invalid_arguments');
    }
  });

  it('maps a throw and an isError result to tool_error, and keeps the tool result', async () => {
    const thrower = { schema: {}, handler: async () => { throw new Error('upstream 500'); } };
    const erring = { schema: {}, handler: async () => ({ isError: true, content: [{ type: 'text', text: 'nope' }] }) };
    const d = deps({ loadRegistry: async () => registryWith({ thrower, erring }) });
    const ctx = capabilityContext('mcp_job');
    const lines = [];
    d.log = (...a) => lines.push(a.join(' '));
    expect((await run(ctx, 'thrower', {}, d)).response.error).toEqual({ code: 'tool_error', message: 'tool failed' });
    expect(lines.join('\n')).not.toContain('upstream 500');
    const { response } = await run(ctx, 'erring', {}, d);
    expect(response.error.code).toBe('tool_error');
    expect(response.result.isError).toBe(true);
  });

  it('audits every refusal with its own outcome, and the transport of the endpoint', async () => {
    const thrower = { schema: {}, handler: async () => { throw new Error('x'); } };
    const d = deps({ endpoint: 'messages', loadRegistry: async () => registryWith({ echo, thrower }) });
    const ctx = capabilityContext('mcp_job');
    expect((await run(ctx, 'nope', {}, d)).row).toMatchObject({ outcome: 'not_found', transport: 'sse' });
    expect((await run(ctx, 'thrower', {}, d)).row).toMatchObject({ outcome: 'tool_error', transport: 'sse' });
    expect((await run(ctx, 'echo', { text: 'a' }, d)).row).toMatchObject({ outcome: 'ok', transport: 'sse' });
  });

  it('consumes a single-use capability: a replay is refused and audited', async () => {
    const d = deps({ context: USER });
    d.reverify = vi.fn(async () => ({ context: USER, single_use: true, permitted_tools: ['echo'] }));
    expect((await run(USER, 'echo', { text: 'a' }, d)).response.ok).toBe(true);
    const { response, row } = await run(USER, 'echo', { text: 'a' }, d);
    expect(response.error.code).toBe('unauthorized');
    expect(row).toMatchObject({ outcome: 'unauthorized', actor: 'user', subject_id: 'u-9' });
    expect(d.consume).toHaveBeenCalledWith('201');
  });

  it('two concurrent presentations of one single-use value: exactly one runs', async () => {
    const d = deps({ context: USER });
    d.reverify = vi.fn(async () => ({ context: USER, single_use: true, permitted_tools: ['echo'] }));
    const results = await Promise.all([1, 2].map(() => executeTool(USER, 'echo', { text: 'a' }, d)));
    expect(results.filter(r => r.ok)).toHaveLength(1);
    expect(results.filter(r => r.error?.code === 'unauthorized')).toHaveLength(1);
  });

  it('never consumes a legacy MCP capability', async () => {
    const d = deps();
    await run(capabilityContext('mcp_job'), 'echo', { text: 'a' }, d);
    await run(capabilityContext('mcp_job'), 'echo', { text: 'a' }, d);
    expect(d.consume).not.toHaveBeenCalled();
  });
});

describe('the SQL the dispatcher writes', () => {
  it('consumes with one conditional update and requires affectedRows 1', async () => {
    expect(CONSUME_CAPABILITY_SQL).toMatch(/consumed_at IS NULL AND revoked_at IS NULL AND expires_at > NOW\(\)/);
    const query = vi.fn().mockResolvedValueOnce([{ affectedRows: 1 }]).mockResolvedValueOnce([{ affectedRows: 0 }]);
    const consume = createConsumer(query);
    expect(await consume('201')).toBe(true);
    expect(await consume('201')).toBe(false);
    expect(query).toHaveBeenCalledWith(CONSUME_CAPABILITY_SQL, ['201']);
  });

  it('audits by capability id and argument digest — no raw value in any column', async () => {
    const query = vi.fn(async () => [{}]);
    const store = createAuditStore(query);
    const d = deps({ audit: store });
    await executeTool(capabilityContext('mcp_job'), 'echo', { text: SECRET }, d);
    const written = JSON.stringify(query.mock.calls);
    expect(written).not.toContain(SECRET);
    expect(query.mock.calls[0][0]).toMatch(/^INSERT INTO tool_invocation/);
    expect(query.mock.calls[1][1][0]).toBe('ok');
  });

  it('digests arguments canonically', () => {
    expect(argsSha256({ a: 1, b: [2, { d: 1, c: 2 }] })).toBe(argsSha256({ b: [2, { c: 2, d: 1 }], a: 1 }));
    expect(argsSha256({ a: 1 })).not.toBe(argsSha256({ a: 2 }));
  });
});

async function mcpPair(registration, context, d) {
  const server = new McpServer({ name: 't', version: '1' });
  for (const [name, t] of registration.tools) server.tool(name, 'd', t.schema, t.handler);
  installToolDispatch(server, registration, context, d);
  const [a, b] = InMemoryTransport.createLinkedPair();
  const client = new Client({ name: 'c', version: '1' });
  await Promise.all([server.connect(a), client.connect(b)]);
  return client;
}

describe('MCP routes tool calls through executeTool', () => {
  it('audits an invalid call the SDK would have rejected before any handler', async () => {
    const d = deps();
    const client = await mcpPair(registryWith({ echo }), capabilityContext('mcp_job'), d);
    const result = await client.callTool({ name: 'echo', arguments: { text: 5 } });
    expect(result.isError).toBe(true);
    expect(result.content[0].text).toMatch(/^invalid_arguments/);
    const rows = [...d.audit.rows.values()];
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({ outcome: 'invalid_arguments', tool_name: 'echo' });
  });

  it('replaces the SDK handler: one call reaches executeTool exactly once', async () => {
    const d = deps();
    const client = await mcpPair(registryWith({ echo }), capabilityContext('mcp_job'), d);
    const result = await client.callTool({ name: 'echo', arguments: { text: 'ok' } });
    expect(result.content[0].text).toBe('ok');
    expect(d.audit.insert).toHaveBeenCalledTimes(1);
    expect((await client.listTools()).tools.map(t => t.name)).toEqual(['echo']);
  });

  it('a tool disabled between two calls in one session is not_found, without reconnect', async () => {
    let enabled = true;
    const d = deps();
    const client = await mcpPair(registryWith({ echo }, { enabled: () => enabled }), capabilityContext('mcp_job'), d);
    expect((await client.callTool({ name: 'echo', arguments: { text: 'a' } })).isError).toBeFalsy();
    enabled = false;
    const second = await client.callTool({ name: 'echo', arguments: { text: 'b' } });
    expect(second.content[0].text).toMatch(/^not_found/);
  });

  it('a session with no tools keeps the SDK default and installs nothing', () => {
    const server = { server: { setRequestHandler: vi.fn() } };
    installToolDispatch(server, registryWith({}), capabilityContext('mcp_job'), deps());
    expect(server.server.setRequestHandler).not.toHaveBeenCalled();
  });
});

async function invokeApp(d, context = USER) {
  const app = express();
  installInvokeRoute(app, {
    guard: (req, _res, next) => { req.capability = context; next(); },
    deps: d,
    loadRegistryFor: () => d.loadRegistry,
    log: vi.fn(),
  });
  const listener = await new Promise(resolve => { const l = app.listen(0, () => resolve(l)); });
  const base = `http://127.0.0.1:${listener.address().port}`;
  const post = (name, body, raw) => fetch(post.url(name), {
    method: 'POST', headers: { 'content-type': 'application/json' }, body: raw ?? JSON.stringify(body),
  });
  post.url = name => `${base}/internal/tools/${name}:invoke`;
  return { post, close: () => listener.close() };
}

describe('POST /internal/tools/{name}:invoke', () => {
  function userDeps() {
    const d = deps({ endpoint: 'invoke', context: USER });
    d.reverify = vi.fn(async () => ({ context: USER, single_use: true, permitted_tools: ['echo'] }));
    return d;
  }

  it('answers {ok, execution_id, result}, and maps each error code to its status', async () => {
    const d = userDeps();
    const { post, close } = await invokeApp(d);
    try {
      const ok = await post('echo', { text: 'hi' });
      expect(ok.status).toBe(200);
      const body = await ok.json();
      expect(body).toMatchObject({ ok: true, result: { content: [{ text: 'hi' }] } });
      expect(body.execution_id).toBeTruthy();
      const replay = await post('echo', { text: 'hi' });
      expect(replay.status).toBe(HTTP_STATUS.unauthorized);
      expect((await replay.json()).error.code).toBe('unauthorized');
      expect(d.reverify).toHaveBeenCalledWith('201', { endpoint: 'invoke' });
      expect([...d.audit.rows.values()].map(r => r.transport)).toEqual(['http', 'http']);
    } finally { close(); }
  });

  it('two concurrent invokes with one value: exactly one 200', async () => {
    const { post, close } = await invokeApp(userDeps());
    try {
      const statuses = (await Promise.all([post('echo', { text: 'a' }), post('echo', { text: 'a' })]))
        .map(r => r.status).sort();
      expect(statuses).toEqual([200, 403]);
    } finally { close(); }
  });

  it('400 for unreadable JSON, and a non-object body is an audited invalid_arguments', async () => {
    const d = userDeps();
    const { post, close } = await invokeApp(d);
    try {
      const broken = await post('echo', null, '{"text":');
      expect(broken.status).toBe(400);
      for (const contentType of ['application/json; charset=bogus', 'application/json']) {
        const res = await fetch(post.url('echo'), {
          method: 'POST', body: '{}',
          headers: { 'content-type': contentType, ...(contentType === 'application/json' ? { 'content-encoding': 'bogus' } : {}) },
        });
        expect(res.status).toBe(400);
        const text = await res.text();
        expect(text).not.toMatch(/at |node_modules|\/Users\/|\.js:/);
        expect(JSON.parse(text).error.code).toBe('invalid_arguments');
      }
      expect(d.audit.insert).not.toHaveBeenCalled();
      const scalar = await post('echo', 'text');
      expect(scalar.status).toBe(400);
      expect([...d.audit.rows.values()][0].outcome).toBe('invalid_arguments');
    } finally { close(); }
  });

  it('an isError body never reaches an invoke caller', async () => {
    const SENTINEL = 'upstream-body-sentinel';
    const erring = { schema: {}, handler: async () => ({ isError: true, content: [{ type: 'text', text: SENTINEL }] }) };
    const d = deps({ endpoint: 'invoke', loadRegistry: async () => registryWith({ erring }) });
    const { post, close } = await invokeApp(d, capabilityContext('mcp_job'));
    try {
      const res = await post('erring', {});
      expect(res.status).toBe(HTTP_STATUS.tool_error);
      const text = await res.text();
      expect(text).not.toContain(SENTINEL);
      expect(JSON.parse(text).error.code).toBe('tool_error');
    } finally { close(); }
  });

  it('a body that is not application/json gets 400: no audit row, no handler call', async () => {
    const handler = vi.fn(async () => ({ content: [] }));
    const d = deps({ endpoint: 'invoke', loadRegistry: async () => registryWith({ noargs: { schema: {}, handler } }) });
    const { post, close } = await invokeApp(d, capabilityContext('mcp_job'));
    try {
      for (const headers of [{ 'content-type': 'text/plain' }, {}]) {
        const res = await fetch(post.url('noargs'), { method: 'POST', headers, body: 'anything' });
        expect(res.status).toBe(400);
        expect((await res.json()).error.code).toBe('invalid_arguments');
      }
      const empties = [
        { headers: { 'content-type': 'application/json' } },
        { headers: { 'content-type': 'application/json', 'content-encoding': 'gzip' }, body: gzipSync(Buffer.alloc(0)) },
        { headers: { 'content-type': 'application/json' }, body: Readable.toWeb(Readable.from([])), duplex: 'half' },
      ];
      for (const init of empties) {
        const empty = await fetch(post.url('noargs'), { method: 'POST', ...init });
        expect(empty.status).toBe(400);
      }
      for (const bom of [Buffer.from([0xef, 0xbb, 0xbf]), Buffer.from([0xff, 0xfe])]) {
        const charset = bom[0] === 0xef ? 'utf-8' : 'utf-16le';
        const res = await fetch(post.url('noargs'), {
          method: 'POST', headers: { 'content-type': `application/json; charset=${charset}` }, body: bom,
        });
        expect(res.status).toBe(400);
      }
      for (const body of ['   ', '{} {}', 'nul']) {
        expect((await post('noargs', null, body)).status).toBe(400);
      }
      const zeros = await rawPost(post.url('noargs'), 'Content-Type: application/json\r\nContent-Length: 00\r\n');
      expect(zeros).toMatch(/^HTTP\/1\.1 400/);
      expect(d.audit.insert).not.toHaveBeenCalled();
      expect(handler).not.toHaveBeenCalled();
      expect((await post('noargs', {})).status).toBe(200);
    } finally { close(); }
  });

  it('does not match a name outside [a-z0-9_] or without the :invoke suffix', async () => {
    const { post, close } = await invokeApp(userDeps());
    try {
      expect((await post('Echo', {})).status).toBe(404);
      expect((await post('../x', {})).status).toBe(404);
    } finally { close(); }
  });
});
