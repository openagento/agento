/**
 * Tests for the Pi -> Toolbox bridge.
 *
 * The bridge lives at `src/agento/modules/pi/bridge/agento-toolbox.js` but its tests
 * live HERE, because `bin/test` runs vitest only from `src/agento/toolbox`. Tests placed
 * next to the bridge would never execute and `bin/test` would still report success.
 *
 * The server double answers the way the real Toolbox was observed to (spike S3): SSE
 * bodies, the session id in the `mcp-session-id` header, 202 for
 * `notifications/initialized`, and tool entries carrying an extra `execution` key.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';

import bridge, {
  isUsableInputSchema,
  normalizeTool,
  parseConfig,
  parseSse,
  sameModel,
  sanitize,
  toAgentToolResult,
  unwrapEnvelope,
} from '../../modules/pi/bridge/agento-toolbox.js';

const SESSION = 'a910e234-1672-406d-9d16-7b9ea2640fd9';

function sse(payload) {
  return `event: message\ndata: ${JSON.stringify(payload)}\n\n`;
}

function schema() {
  return { type: 'object', properties: { q: { type: 'string' } }, required: ['q'] };
}

/** A Toolbox stand-in that speaks the observed wire format. */
function makeServer({ tools = null, onCall = null, overrides = {} } = {}) {
  const calls = [];
  const toolList = tools ?? [
    // The extra `execution` key is present on the real server; a validator that
    // rejected unknown keys would drop every tool.
    { name: 'jira_search', description: 'Search', inputSchema: schema(), execution: 'remote' },
  ];

  const handler = vi.fn(async (url, init = {}) => {
    const body = init.body ? JSON.parse(init.body) : null;
    calls.push({ url, method: init.method, headers: init.headers, body, signal: init.signal });

    if (init.method === 'DELETE') return new Response('', { status: 200 });

    const method = body?.method;
    if (overrides[method]) return overrides[method](body, calls);

    if (method === 'initialize') {
      return new Response(
        sse({ jsonrpc: '2.0', id: body.id, result: { protocolVersion: '2025-06-18' } }),
        { status: 200, headers: { 'content-type': 'text/event-stream', 'mcp-session-id': SESSION } },
      );
    }
    if (method === 'notifications/initialized') return new Response('', { status: 202 });
    if (method === 'tools/list') {
      return new Response(sse({ jsonrpc: '2.0', id: body.id, result: { tools: toolList } }), {
        status: 200,
        headers: { 'content-type': 'text/event-stream' },
      });
    }
    if (method === 'tools/call') {
      const result = onCall
        ? onCall(body)
        : { content: [{ type: 'text', text: 'ok' }] };
      return new Response(sse({ jsonrpc: '2.0', id: body.id, result }), {
        status: 200,
        headers: { 'content-type': 'text/event-stream' },
      });
    }
    return new Response('{}', { status: 500 });
  });

  return { handler, calls };
}

/**
 * A double of the PINNED Pi ExtensionAPI (core/extensions/types.d.ts):
 *   appendEntry(customType, data)   :936   — NOT ctx.session.appendCustom
 *   getAllTools() / registerTool() / on()
 * `hasUI` lives on the ExtensionContext handed to handlers, not on `pi`.
 *
 * An earlier version of this file invented `ctx.session.appendCustom`, so the bridge's
 * call to a nonexistent API passed its own test. Model the real surface only.
 */
function makePi() {
  const tools = new Map();
  const handlers = new Map();
  const entries = [];
  let loading = true; // action methods throw until the runner binds the real runtime
  const actionMethod = (name) => () => {
    if (loading) {
      throw new Error(
        'Extension runtime not initialized. Action methods cannot be called during ' +
          `extension loading. (${name})`,
      );
    }
  };
  return {
    tools,
    handlers,
    entries,
    // Only registerTool() is valid during load — loader.js:154 says so explicitly.
    registerTool: (def) => tools.set(def.name, def),
    on: (event, fn) => handlers.set(event, fn),
    // These are ACTION METHODS: loader.js:135-155 installs throwing stubs for them until
    // Runner.bindCore() replaces them. A double that implements them as working functions
    // hides a bridge that cannot load at all — which is exactly what happened.
    getAllTools: actionMethod('getAllTools'),
    sendMessage: actionMethod('sendMessage'),
    setModel: actionMethod('setModel'),
    appendEntry: (customType, data) => {
      if (loading) actionMethod('appendEntry')();
      entries.push({ customType, data });
    },
    /** Called by the test once loading is over, mirroring Runner.bindCore(). */
    _bind() {
      loading = false;
    },
  };
}

/** The ExtensionContext shape handlers actually receive. */
function makeCtx({ hasUI = false } = {}) {
  return { hasUI };
}

let cwdSpy;
let readFileMock;

vi.mock('node:fs/promises', () => ({
  readFile: (...args) => readFileMock(...args),
}));

beforeEach(() => {
  readFileMock = vi.fn(async () => JSON.stringify({ url: 'http://toolbox:3001/mcp?agent_view_id=1' }));
  cwdSpy = vi.spyOn(process, 'cwd').mockReturnValue('/run');
  process.exitCode = undefined;
});

afterEach(() => {
  cwdSpy.mockRestore();
  vi.unstubAllGlobals();
  process.exitCode = undefined;
});

describe('config validation', () => {
  it('requires a non-empty url', () => {
    expect(() => parseConfig({})).toThrow(/url is required/);
    expect(() => parseConfig({ url: '   ' })).toThrow(/url is required/);
  });

  it('rejects a non-positive timeout', () => {
    expect(() => parseConfig({ url: 'u', timeout_ms: 0 })).toThrow(/positive integer/);
  });

  it('warns but does not fail on an unknown key, so an older bridge keeps working', () => {
    const warn = vi.spyOn(console, 'error').mockImplementation(() => {});
    const cfg = parseConfig({ url: 'u', future_option: true });
    expect(cfg.url).toBe('u');
    expect(warn).toHaveBeenCalledWith(expect.stringContaining('future_option'));
    warn.mockRestore();
  });

  it('applies defaults and carries the model-guard fields', () => {
    const cfg = parseConfig({ url: 'u', expected_model: 'm', expected_provider: 'p' });
    expect(cfg.timeoutMs).toBe(120000);
    expect(cfg.expectedModel).toBe('m');
    expect(cfg.expectedProvider).toBe('p');
  });

  it('defaults to ENFORCING the model identity', () => {
    // Absent marker must mean "enforce": the opt-out has to be opted INTO, or a
    // connection file from an older build silently loses its guard.
    expect(parseConfig({ url: 'u' }).allowModelSubstitution).toBe(false);
  });

  it('accepts the substitution marker as a boolean', () => {
    expect(parseConfig({ url: 'u', allow_model_substitution: true }).allowModelSubstitution)
      .toBe(true);
    expect(parseConfig({ url: 'u', allow_model_substitution: false }).allowModelSubstitution)
      .toBe(false);
  });

  it('rejects a non-boolean marker rather than guessing', () => {
    // Same reasoning as a malformed expectation: a truthy string like "0" would read as
    // "substitution allowed" and disable the guard at its own config trust boundary.
    for (const bad of ['1', '0', 1, 0, null, {}]) {
      expect(() => parseConfig({ url: 'u', allow_model_substitution: bad }))
        .toThrow(/allow_model_substitution must be a boolean/);
    }
  });

  it('does not warn about the marker as an unknown key', () => {
    const warn = vi.spyOn(console, 'error').mockImplementation(() => {});
    parseConfig({ url: 'u', allow_model_substitution: true });
    expect(warn).not.toHaveBeenCalled();
    warn.mockRestore();
  });
});

describe('SSE parsing', () => {
  it('reads a single frame', () => {
    expect(parseSse(sse({ a: 1 }))).toEqual({ a: 1 });
  });

  it('joins multi-line data', () => {
    expect(parseSse('data: {"a":\ndata: 1}\n\n')).toEqual({ a: 1 });
  });

  it('skips comments and picks the first JSON frame of several', () => {
    const body = `: keepalive\n\n${sse({ first: true })}${sse({ second: true })}`;
    expect(parseSse(body)).toEqual({ first: true });
  });
});

describe('JSON-RPC envelope', () => {
  it('rejects a mismatched id', () => {
    expect(() => unwrapEnvelope({ jsonrpc: '2.0', id: 9, result: {} }, 1)).toThrow(/does not match/);
  });

  it('rejects both result and error, or neither', () => {
    expect(() => unwrapEnvelope({ jsonrpc: '2.0', id: 1 }, 1)).toThrow(/exactly one/);
    expect(() => unwrapEnvelope({ jsonrpc: '2.0', id: 1, result: {}, error: {} }, 1)).toThrow(/exactly one/);
  });

  it('survives an error object with no code or message', () => {
    expect(() => unwrapEnvelope({ jsonrpc: '2.0', id: 1, error: {} }, 1)).toThrow(
      /server error unknown: unknown error/,
    );
  });
});

describe('inputSchema validation', () => {
  it('accepts an object schema and tolerates unknown keys', () => {
    expect(isUsableInputSchema({ type: 'object', execution: 'remote' })).toBe(true);
  });

  it.each([
    ['an array', []],
    ['null', null],
    ['type array', { type: 'array' }],
    ['properties as array', { type: 'object', properties: [] }],
    ['required with non-strings', { type: 'object', required: [1, 2] }],
  ])('rejects %s', (_label, value) => {
    expect(isUsableInputSchema(value)).toBe(false);
  });
});

describe('tool normalization', () => {
  it('prefixes the name', () => {
    expect(normalizeTool({ name: 'jira_search', inputSchema: schema() }).piName).toBe(
      'mcp__toolbox__jira_search',
    );
  });

  it('accepts a Toolbox tool named bash — the prefix makes shadowing impossible', () => {
    const tool = normalizeTool({ name: 'bash', inputSchema: schema() });
    expect(tool.skip).toBeUndefined();
    expect(tool.piName).toBe('mcp__toolbox__bash');
  });

  it.each([
    ['no name', {}],
    ['a space', { name: 'a b' }],
    ['a slash', { name: 'a/b' }],
    ['a newline', { name: 'a\nb' }],
    ['unicode', { name: 'zażółć' }],
    ['over 50 chars', { name: `a${'b'.repeat(60)}` }],
  ])('skips a name with %s', (_label, entry) => {
    expect(normalizeTool({ inputSchema: schema(), ...entry }).skip).toBeTruthy();
  });
});

describe('tools/call result mapping', () => {
  it('returns an AgentToolResult, not a bare string', () => {
    const out = toAgentToolResult({ content: [{ type: 'text', text: 'hi' }] });
    expect(out).toEqual({ content: [{ type: 'text', text: 'hi' }], details: {} });
  });

  it('THROWS on isError — returning a value would read as success to the model', () => {
    expect(() => toAgentToolResult({ isError: true, content: [{ type: 'text', text: 'boom' }] })).toThrow(
      /boom/,
    );
  });

  it('treats a non-boolean isError as an error', () => {
    expect(() => toAgentToolResult({ isError: 'yes', content: [] })).toThrow();
  });

  it('rejects a missing content array', () => {
    expect(() => toAgentToolResult({})).toThrow(/no content array/);
  });
});

describe('sanitisation', () => {
  it('strips the toolbox url and session id from text shown to the model', () => {
    const cfg = { url: 'http://toolbox:3001/mcp' };
    const out = sanitize(`failed calling http://toolbox:3001/mcp with ${SESSION}`, cfg);
    expect(out).not.toContain('toolbox:3001');
    expect(out).not.toContain(SESSION);
  });
});

describe('factory: handshake and registration', () => {
  it('registers prefixed tools and completes the documented handshake', async () => {
    const { handler, calls } = makeServer();
    vi.stubGlobal('fetch', handler);
    const pi = makePi();

    await bridge(pi);

    expect([...pi.tools.keys()]).toEqual(['mcp__toolbox__jira_search']);
    const methods = calls.map((c) => c.body?.method).filter(Boolean);
    expect(methods).toEqual(['initialize', 'notifications/initialized', 'tools/list']);
    // The negotiated version must be echoed on every request after initialize.
    expect(calls[1].headers['MCP-Protocol-Version']).toBe('2025-06-18');
    expect(calls[1].headers['Mcp-Session-Id']).toBe(SESSION);
  });

  it('executes a tool and passes the ORIGINAL (unprefixed) name upstream', async () => {
    const { handler, calls } = makeServer();
    vi.stubGlobal('fetch', handler);
    const pi = makePi();
    await bridge(pi);

    const out = await pi.tools.get('mcp__toolbox__jira_search').execute('c1', { q: 'x' }, undefined);
    expect(out).toEqual({ content: [{ type: 'text', text: 'ok' }], details: {} });
    const call = calls.find((c) => c.body?.method === 'tools/call');
    expect(call.body.params.name).toBe('jira_search');
  });

  it('a tool error surfaces as a throw, sanitised', async () => {
    const { handler } = makeServer({
      onCall: () => ({ isError: true, content: [{ type: 'text', text: 'upstream said no' }] }),
    });
    vi.stubGlobal('fetch', handler);
    const pi = makePi();
    await bridge(pi);

    await expect(
      pi.tools.get('mcp__toolbox__jira_search').execute('c1', {}, undefined),
    ).rejects.toThrow(/upstream said no/);
  });

  it('passes Pi\'s cancellation signal through to fetch', async () => {
    const { handler, calls } = makeServer();
    vi.stubGlobal('fetch', handler);
    const pi = makePi();
    await bridge(pi);

    const controller = new AbortController();
    await pi.tools.get('mcp__toolbox__jira_search').execute('c1', {}, controller.signal);
    const call = calls.find((c) => c.body?.method === 'tools/call');
    expect(call.signal).toBeInstanceOf(AbortSignal);
  });

  it('skips a duplicate final name instead of registering it twice', async () => {
    const { handler } = makeServer({
      tools: [
        { name: 'dup', inputSchema: schema() },
        { name: 'dup', inputSchema: schema() },
      ],
    });
    vi.stubGlobal('fetch', handler);
    const warn = vi.spyOn(console, 'error').mockImplementation(() => {});
    const pi = makePi();
    await bridge(pi);

    expect(pi.tools.size).toBe(1);
    expect(warn).toHaveBeenCalledWith(expect.stringContaining('duplicate'));
    warn.mockRestore();
  });

  it('skips a malformed entry rather than failing the whole run', async () => {
    const { handler } = makeServer({
      tools: [{ name: 'good', inputSchema: schema() }, { name: 'bad', inputSchema: { type: 'array' } }],
    });
    vi.stubGlobal('fetch', handler);
    const warn = vi.spyOn(console, 'error').mockImplementation(() => {});
    const pi = makePi();
    await bridge(pi);

    expect([...pi.tools.keys()]).toEqual(['mcp__toolbox__good']);
    warn.mockRestore();
  });
});

describe('factory: fatal failures', () => {
  it('THROWS when the toolbox is unreachable — the run must not proceed toolless', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('ECONNREFUSED'); }));
    await expect(bridge(makePi())).rejects.toThrow(/transport failure/);
  });

  it('THROWS when initialize returns no session header', async () => {
    const { handler } = makeServer({
      overrides: {
        initialize: (body) =>
          new Response(sse({ jsonrpc: '2.0', id: body.id, result: { protocolVersion: '2025-06-18' } }), {
            status: 200,
            headers: { 'content-type': 'text/event-stream' },
          }),
      },
    });
    vi.stubGlobal('fetch', handler);
    await expect(bridge(makePi())).rejects.toThrow(/no Mcp-Session-Id/);
  });

  it('THROWS on an unsupported protocolVersion', async () => {
    const { handler } = makeServer({
      overrides: {
        initialize: (body) =>
          new Response(sse({ jsonrpc: '2.0', id: body.id, result: { protocolVersion: '1999-01-01' } }), {
            status: 200,
            headers: { 'content-type': 'text/event-stream', 'mcp-session-id': SESSION },
          }),
      },
    });
    vi.stubGlobal('fetch', handler);
    await expect(bridge(makePi())).rejects.toThrow(/unsupported protocolVersion/);
  });

  it('THROWS on a repeated pagination cursor instead of looping forever', async () => {
    const { handler } = makeServer({
      overrides: {
        'tools/list': (body) =>
          new Response(
            sse({ jsonrpc: '2.0', id: body.id, result: { tools: [], nextCursor: 'same' } }),
            { status: 200, headers: { 'content-type': 'text/event-stream' } },
          ),
      },
    });
    vi.stubGlobal('fetch', handler);
    await expect(bridge(makePi())).rejects.toThrow(/repeated a pagination cursor/);
  });

  it('DELETEs the session when the factory fails after initialize', async () => {
    const { handler, calls } = makeServer({
      overrides: {
        'tools/list': (body) =>
          new Response(sse({ jsonrpc: '2.0', id: body.id, result: { nope: true } }), {
            status: 200,
            headers: { 'content-type': 'text/event-stream' },
          }),
      },
    });
    vi.stubGlobal('fetch', handler);
    await expect(bridge(makePi())).rejects.toThrow(/tools array/);
    // Without this, the server-side session survives to container restart:
    // `session_shutdown` never runs because the Pi session never started.
    expect(calls.some((c) => c.method === 'DELETE')).toBe(true);
  });

  it('THROWS when the config file is missing', async () => {
    readFileMock = vi.fn(async () => { throw new Error('ENOENT'); });
    vi.stubGlobal('fetch', vi.fn());
    await expect(bridge(makePi())).rejects.toThrow(/cannot read/);
  });
});

describe('lifecycle', () => {
  it('DELETEs the session on session_shutdown', async () => {
    const { handler, calls } = makeServer();
    vi.stubGlobal('fetch', handler);
    const pi = makePi();
    await bridge(pi);

    pi._bind();
    await pi.handlers.get('session_shutdown')();
    expect(calls.filter((c) => c.method === 'DELETE')).toHaveLength(1);

    // Idempotent: a second shutdown must not fire another DELETE.
    await pi.handlers.get('session_shutdown')();
    expect(calls.filter((c) => c.method === 'DELETE')).toHaveLength(1);
  });

  it('records the init report via pi.appendEntry, the pinned API', async () => {
    const { handler } = makeServer();
    vi.stubGlobal('fetch', handler);
    const pi = makePi();
    await bridge(pi);

    pi._bind(); // handlers run after Runner.bindCore(), when action methods are live
    await pi.handlers.get('session_start')({}, makeCtx());
    expect(pi.entries).toHaveLength(1);
    expect(pi.entries[0].customType).toBe('agento-toolbox-init');
    expect(pi.entries[0].data.tools).toEqual(['mcp__toolbox__jira_search']);
  });
});

describe('model guard', () => {
  async function runGuard(configExtra, message, { hasUI = false } = {}) {
    readFileMock = vi.fn(async () =>
      JSON.stringify({ url: 'http://toolbox:3001/mcp', ...configExtra }),
    );
    const { handler } = makeServer();
    vi.stubGlobal('fetch', handler);
    const warn = vi.spyOn(console, 'error').mockImplementation(() => {});
    const pi = makePi();
    await bridge(pi);
    pi._bind();
    // Handlers receive (event, ctx); hasUI is on the CONTEXT, not on `pi`.
    await pi.handlers.get('message_end')({ message }, makeCtx({ hasUI }));
    warn.mockRestore();
    return pi;
  }

  it('sets a non-zero exit code when a different model ran', async () => {
    // Pi resolves an unmatched model by SILENT substring matching, so without this a
    // wrong-model run would exit 0 and read as success to any script or CI job.
    await runGuard(
      { expected_model: 'wanted', expected_provider: 'openrouter' },
      { role: 'assistant', model: 'other', provider: 'openrouter' },
    );
    expect(process.exitCode).toBe(1);
  });

  it('stays silent when the right model ran', async () => {
    await runGuard(
      { expected_model: 'wanted', expected_provider: 'openrouter' },
      { role: 'assistant', model: 'wanted', provider: 'openrouter' },
    );
    expect(process.exitCode).toBeUndefined();
  });

  it('is inert when no expectation is configured', async () => {
    await runGuard({}, { role: 'assistant', model: 'anything', provider: 'p' });
    expect(process.exitCode).toBeUndefined();
  });

  it('records a mismatch entry the runner can act on', async () => {
    const pi = await runGuard(
      { expected_model: 'wanted', expected_provider: 'openrouter' },
      { role: 'assistant', model: 'other', provider: 'openrouter' },
    );
    const entry = pi.entries.find((e) => e.customType === 'agento-model-mismatch');
    expect(entry).toBeDefined();
    expect(entry.data).toMatchObject({ expectedModel: 'wanted', actualModel: 'other' });
  });

  it('catches a wrong PROVIDER too, not just a wrong model', async () => {
    await runGuard(
      { expected_model: 'wanted', expected_provider: 'openrouter' },
      { role: 'assistant', model: 'wanted', provider: 'somewhere-else' },
    );
    expect(process.exitCode).toBe(1);
  });

  it('warns but does not fail the exit code in an interactive session', async () => {
    await runGuard(
      { expected_model: 'wanted', expected_provider: 'openrouter' },
      { role: 'assistant', model: 'other', provider: 'openrouter' },
      { hasUI: true },
    );
    expect(process.exitCode).toBeUndefined();
  });

  // The router opt-out is an EXPLICIT marker. It used to be inferred from a missing
  // `expected_model`, but absence has a second cause — an agent_view with no model
  // configured — so inferring it disabled the guard for a case that wanted it on.
  it('allows a substituted model when the marker is set', async () => {
    await runGuard(
      {
        expected_model: 'openrouter/free',
        expected_provider: 'openrouter',
        allow_model_substitution: true,
      },
      { role: 'assistant', model: 'poolside/laguna-xs-2.1:free', provider: 'openrouter' },
    );
    expect(process.exitCode).toBeUndefined();
  });

  it('still catches a wrong PROVIDER when substitution is allowed', async () => {
    // A router substitutes the model by design; it never changes the vendor.
    await runGuard(
      {
        expected_model: 'openrouter/free',
        expected_provider: 'openrouter',
        allow_model_substitution: true,
      },
      { role: 'assistant', model: 'anything', provider: 'somewhere-else' },
    );
    expect(process.exitCode).toBe(1);
  });

  it('enforces the model when the marker is absent (legacy connection file)', async () => {
    // A build written before the key existed must behave exactly as it did.
    await runGuard(
      { expected_model: 'wanted', expected_provider: 'openrouter' },
      { role: 'assistant', model: 'other', provider: 'openrouter' },
    );
    expect(process.exitCode).toBe(1);
  });

  it('enforces the model when the marker is explicitly false', async () => {
    await runGuard(
      {
        expected_model: 'wanted',
        expected_provider: 'openrouter',
        allow_model_substitution: false,
      },
      { role: 'assistant', model: 'other', provider: 'openrouter' },
    );
    expect(process.exitCode).toBe(1);
  });
});

describe('model guard fails CLOSED', () => {
  async function guard(configExtra, message, { hasUI = false } = {}) {
    readFileMock = vi.fn(async () =>
      JSON.stringify({ url: 'http://toolbox:3001/mcp', ...configExtra }),
    );
    const { handler } = makeServer();
    vi.stubGlobal('fetch', handler);
    const warn = vi.spyOn(console, 'error').mockImplementation(() => {});
    const pi = makePi();
    await bridge(pi);
    pi._bind();
    await pi.handlers.get('message_end')({ message }, makeCtx({ hasUI }));
    warn.mockRestore();
    return pi;
  }

  it('a MISSING actual model is a mismatch, not a pass', async () => {
    // Requiring the actual field to be truthy before comparing let an assistant message
    // reporting no identity sail through — the shape a silent substitution takes.
    await guard(
      { expected_model: 'wanted', expected_provider: 'openrouter' },
      { role: 'assistant', provider: 'openrouter' },
    );
    expect(process.exitCode).toBe(1);
  });

  it('a MISSING actual provider is a mismatch too', async () => {
    await guard(
      { expected_model: 'wanted', expected_provider: 'openrouter' },
      { role: 'assistant', model: 'wanted' },
    );
    expect(process.exitCode).toBe(1);
  });

  it.each([
    ['a non-string', { expected_model: 42 }],
    ['an empty string', { expected_model: '' }],
    ['whitespace only', { expected_provider: '   ' }],
  ])('REJECTS %s expectation instead of silently disabling the guard', (_l, extra) => {
    expect(() => parseConfig({ url: 'u', ...extra })).toThrow(/non-empty string/);
  });
});

describe('sameModel — the `~` alias marker (found live by spike S2)', () => {
  it('treats a ~-prefixed actual as the requested model', () => {
    // Requesting `anthropic/claude-haiku-latest` makes Pi report
    // `~anthropic/claude-haiku-latest`; the `~` marks a catalogue alias. Strict equality
    // failed a legitimate run and would have broken every aliased model.
    expect(sameModel('~anthropic/claude-haiku-latest', 'anthropic/claude-haiku-latest')).toBe(true);
    expect(sameModel('anthropic/claude-haiku-latest', '~anthropic/claude-haiku-latest')).toBe(true);
    expect(sameModel('~~x/y', 'x/y')).toBe(true);
  });

  it('still rejects a genuinely different model', () => {
    // Normalising the marker must not weaken the real check.
    expect(sameModel('~openai/gpt-latest', 'anthropic/claude-haiku-latest')).toBe(false);
    expect(sameModel('openai/gpt-5.4-mini:batch', 'gpt-5.4-mini')).toBe(false);
  });

  it('rejects non-strings rather than coercing them', () => {
    expect(sameModel(undefined, 'x')).toBe(false);
    expect(sameModel('x', undefined)).toBe(false);
    expect(sameModel(null, null)).toBe(false);
  });

  it('the guard accepts an aliased model end to end', async () => {
    readFileMock = vi.fn(async () =>
      JSON.stringify({
        url: 'http://toolbox:3001/mcp',
        expected_provider: 'openrouter',
        expected_model: 'anthropic/claude-haiku-latest',
      }),
    );
    const { handler } = makeServer();
    vi.stubGlobal('fetch', handler);
    const pi = makePi();
    await bridge(pi);
    pi._bind();
    await pi.handlers.get('message_end')(
      {
        message: {
          role: 'assistant',
          provider: 'openrouter',
          model: '~anthropic/claude-haiku-latest',
        },
      },
      makeCtx(),
    );
    expect(process.exitCode).toBeUndefined();
    expect(pi.entries.find((e) => e.customType === 'agento-model-mismatch')).toBeUndefined();
  });

  it('a router dispatch passes when no expected_model is configured', async () => {
    // A connection file with no `expected_model` at all leaves the model half of the guard
    // inactive, and the PROVIDER check must still apply. NOTE: this is no longer how the
    // router opt-out is expressed — `pi/allow_model_substitution=1` now writes an explicit
    // `allow_model_substitution: true` and KEEPS `expected_model` (see the marker tests
    // above). This case still matters because an agent_view with no model configured
    // produces exactly this shape.
    readFileMock = vi.fn(async () =>
      JSON.stringify({ url: 'http://toolbox:3001/mcp', expected_provider: 'openrouter' }),
    );
    const { handler } = makeServer();
    vi.stubGlobal('fetch', handler);
    const pi = makePi();
    await bridge(pi);
    pi._bind();
    await pi.handlers.get('message_end')(
      { message: { role: 'assistant', provider: 'openrouter', model: 'poolside/laguna-xs-2.1:free' } },
      makeCtx(),
    );
    expect(process.exitCode).toBeUndefined();

    // …but a wrong PROVIDER is still caught.
    await pi.handlers.get('message_end')(
      { message: { role: 'assistant', provider: 'somewhere-else', model: 'anything' } },
      makeCtx(),
    );
    expect(process.exitCode).toBe(1);
  });
});
