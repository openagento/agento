import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import express from 'express';
import { TextDecoder } from 'node:util';
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { SSEServerTransport } from '@modelcontextprotocol/sdk/server/sse.js';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { SSEClientTransport } from '@modelcontextprotocol/sdk/client/sse.js';
import { createRequireCapability, extractToken, tokenHash } from '../capability.js';
import { capabilityContext } from './capability-rows.js';

// The `/sse` + `/messages` wiring from server.js, mounted on an ephemeral app with a fake
// verifier. It uses the REAL SSEServerTransport and the REAL guard, so what this proves about
// the endpoint the SDK advertises and the credential the SDK sends holds for the deployment.
//
// A behavioural test, not a `fetch()` script: the defect this guards against (`/messages`
// answering 401 to every legitimate client) is invisible to a hand-written POST, because a
// hand-written POST can add an Authorization header the real client never sends.
const TOKEN_A = 'cap-token-a';
const TOKEN_B = 'cap-token-b';
const CLAIMS = {
  [TOKEN_A]: { context: capabilityContext('mcp_interactive', { agent_view_id: 1 }) },
  [TOKEN_B]: { context: capabilityContext('mcp_interactive', { agent_view_id: 2 }) },
};
const noopLog = () => {};

let server;
let baseUrl;
const sessions = new Map();
const advertisedEndpoints = [];

beforeAll(async () => {
  const app = express();
  const guard = createRequireCapability(
    async token => CLAIMS[token] || null,
    { endpoint: 'sse' },
    noopLog
  );

  app.get('/sse', guard, async (req, res) => {
    const mcp = new McpServer({ name: 'test', version: '1.0.0' });
    mcp.tool('whoami', 'view probe', {}, async () => ({
      content: [{ type: 'text', text: `view ${req.capability.agent_view_id}` }],
    }));
    const connectToken = extractToken(req);
    const transport = new SSEServerTransport(
      `/messages?cap=${encodeURIComponent(connectToken)}`,
      res
    );
    sessions.set(transport.sessionId, { transport, capabilityHash: tokenHash(connectToken) });
    res.on('close', () => {
      sessions.delete(transport.sessionId);
      mcp.close().catch(() => {});
    });
    await mcp.connect(transport);
  });

  app.post('/messages', guard, async (req, res) => {
    const entry = sessions.get(req.query.sessionId);
    if (!entry) return res.status(400).json({ error: 'Unknown session' });
    if (entry.capabilityHash !== tokenHash(extractToken(req))) {
      return res.status(403).json({ error: 'capability does not own this session' });
    }
    return entry.transport.handlePostMessage(req, res);
  });

  await new Promise(resolve => {
    server = app.listen(0, '127.0.0.1', resolve);
  });
  baseUrl = `http://127.0.0.1:${server.address().port}`;
});

afterAll(async () => {
  if (server) await new Promise(resolve => server.close(resolve));
});

// Capture the endpoint the server advertises, exactly as the SDK client reads it.
async function readAdvertisedEndpoint(token) {
  const res = await fetch(`${baseUrl}/sse?cap=${encodeURIComponent(token)}`);
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffered = '';
  let endpoint = null;
  while (!endpoint) {
    const { value, done } = await reader.read();
    if (done) break;
    buffered += decoder.decode(value, { stream: true });
    const m = buffered.match(/^data: (\/messages\?\S+)$/m);
    if (m) endpoint = m[1];
  }
  advertisedEndpoints.push({ endpoint, cancel: () => reader.cancel().catch(() => {}) });
  return endpoint;
}

describe('SSE transport authentication', () => {
  it('the installed SSE client connects and calls a tool with NO configured headers', async () => {
    // `requestInit` is deliberately absent: the generated Claude/Codex MCP configs and
    // bin/test-toolbox.sh all pass a bare URL, so the POST to `/messages` carries no
    // Authorization header at all. It authenticates through the advertised endpoint.
    const transport = new SSEClientTransport(new URL(`${baseUrl}/sse?cap=${TOKEN_A}`));
    const client = new Client({ name: 'probe', version: '1.0.0' });
    await client.connect(transport);
    try {
      const result = await client.callTool({ name: 'whoami', arguments: {} });
      expect(result.content[0].text).toBe('view 1');
    } finally {
      await client.close();
    }
  });

  it('advertises an endpoint carrying both the capability and the session id', async () => {
    const endpoint = await readAdvertisedEndpoint(TOKEN_A);
    const url = new URL(endpoint, baseUrl);
    expect(url.searchParams.get('cap')).toBe(TOKEN_A);
    expect(url.searchParams.get('sessionId')).toBeTruthy();
  });

  it('rejects the same endpoint with the capability stripped', async () => {
    const endpoint = await readAdvertisedEndpoint(TOKEN_A);
    const url = new URL(endpoint, baseUrl);
    url.searchParams.delete('cap');
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: '{"jsonrpc":"2.0","id":1,"method":"ping"}',
    });
    expect(res.status).toBe(401);
  });

  it("rejects another view's capability on this session", async () => {
    const endpoint = await readAdvertisedEndpoint(TOKEN_A);
    const url = new URL(endpoint, baseUrl);
    const res = await fetch(url, {
      method: 'POST',
      // A header beats `?cap=` in extractToken, so this is B driving A's session.
      headers: { 'content-type': 'application/json', authorization: `Bearer ${TOKEN_B}` },
      body: '{"jsonrpc":"2.0","id":1,"method":"ping"}',
    });
    expect(res.status).toBe(403);
  });
});

afterAll(() => {
  for (const { cancel } of advertisedEndpoints) cancel();
});
