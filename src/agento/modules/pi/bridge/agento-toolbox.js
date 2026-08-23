/**
 * Agento Toolbox bridge for the Pi coding agent.
 *
 * Pi ships no MCP client ("No MCP. Build CLI tools with READMEs, or build an extension
 * that adds MCP support." — its own docs), so this extension is the MCP client: it
 * speaks Streamable HTTP to the Toolbox and registers each Toolbox tool as a native Pi
 * tool.
 *
 * ZERO RUNTIME DEPENDENCIES, deliberately. Pi loads this file by path from the per-job
 * build directory, and Node's resolver walks up from *that* directory looking for
 * node_modules — it never reaches the globally installed Pi's own modules. So neither
 * `@modelcontextprotocol/sdk` nor `zod` can be imported here. Validation is therefore
 * hand-written and total; see `RULES.md`, section "Agent extensions loaded outside
 * node_modules".
 *
 * Names are `mcp__toolbox__<tool>` so app_monitor's existing
 * `name.startsWith('mcp__toolbox__')` telemetry keeps working with no change.
 *
 * The contract below is not inferred — it was verified against a live Toolbox
 * (spike S3): responses are `text/event-stream`, the session id arrives as the
 * `mcp-session-id` HEADER, `notifications/initialized` answers 202, `tools/list`
 * returns real JSON Schema, tool entries carry an extra `execution` key beyond the
 * spec'd fields (so unknown keys MUST be tolerated), and `DELETE` genuinely frees the
 * session (reusing the id afterwards yields 400 "Server not initialized").
 */

import { readFile } from 'node:fs/promises';
import path from 'node:path';

const TOOL_PREFIX = 'mcp__toolbox__';
const PROTOCOL_VERSION = '2025-06-18';
const SUPPORTED_PROTOCOL_VERSIONS = new Set(['2025-06-18', '2025-03-26', '2024-11-05']);
const CONFIG_FILENAME = '.pi/agento-toolbox.json';
const INIT_RECORD = 'agento-toolbox-init';
const MISMATCH_RECORD = 'agento-model-mismatch';

// Prefix costs 14 chars; Pi does not validate tool names, so we enforce the 64-char
// ceiling ourselves or the provider request fails deep inside a turn.
const MAX_TOOL_NAME = 64;
const MAX_RAW_NAME = MAX_TOOL_NAME - TOOL_PREFIX.length;
const TOOL_NAME_RE = /^[a-z][a-z0-9_]*$/;

const DEFAULTS = {
  timeoutMs: 120000,
  handshakeTimeoutMs: 30000,
  maxToolPages: 50,
  // Enforce the model identity unless the connection file explicitly opts out.
  allowModelSubstitution: false,
};

/** Fail loudly and identifiably from the factory — Pi turns that into `exit 1`. */
class BridgeError extends Error {
  constructor(message) {
    super(`[agento-toolbox] ${message}`);
    this.name = 'BridgeError';
  }
}

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

/**
 * Validate the connection file. Unknown keys warn rather than throw so an older bridge
 * keeps working against a newer file; a missing/blank `url` is fatal because there is
 * nothing sensible to do without it.
 */
export function parseConfig(raw) {
  if (raw === null || typeof raw !== 'object' || Array.isArray(raw)) {
    throw new BridgeError('config must be a JSON object');
  }
  const url = raw.url;
  if (typeof url !== 'string' || url.trim() === '') {
    throw new BridgeError('config.url is required and must be a non-empty string');
  }

  const cfg = { url: url.trim(), headers: {}, ...DEFAULTS };

  if (raw.headers !== undefined) {
    if (raw.headers === null || typeof raw.headers !== 'object' || Array.isArray(raw.headers)) {
      throw new BridgeError('config.headers must be an object of string values');
    }
    for (const [key, value] of Object.entries(raw.headers)) {
      if (typeof value !== 'string') {
        throw new BridgeError(`config.headers.${key} must be a string`);
      }
      cfg.headers[key] = value;
    }
  }

  const ints = {
    timeout_ms: 'timeoutMs',
    handshake_timeout_ms: 'handshakeTimeoutMs',
    max_tool_pages: 'maxToolPages',
  };
  for (const [jsonKey, cfgKey] of Object.entries(ints)) {
    if (raw[jsonKey] === undefined) continue;
    const value = raw[jsonKey];
    if (!Number.isInteger(value) || value <= 0) {
      throw new BridgeError(`config.${jsonKey} must be a positive integer`);
    }
    cfg[cfgKey] = value;
  }

  const known = new Set([
    'url',
    'headers',
    'expected_provider',
    'expected_model',
    'allow_model_substitution',
    ...Object.keys(ints),
  ]);
  for (const key of Object.keys(raw)) {
    if (!known.has(key)) {
      console.error(`[agento-toolbox] warning: ignoring unknown config key '${key}'`);
    }
  }

  // A malformed expectation must be rejected, not dropped: silently ignoring it disables
  // the model guard at its own config trust boundary, which is the failure this guard
  // exists to prevent.
  for (const key of ['expected_provider', 'expected_model']) {
    if (raw[key] === undefined) continue;
    if (typeof raw[key] !== 'string' || raw[key].trim() === '') {
      throw new BridgeError(`config.${key} must be a non-empty string when present`);
    }
  }
  if (raw.expected_provider) cfg.expectedProvider = raw.expected_provider.trim();
  if (raw.expected_model) cfg.expectedModel = raw.expected_model.trim();

  // A router/meta model (`pi/allow_model_substitution`) dispatches to another model BY
  // DESIGN, so its identity must not be enforced. This is an EXPLICIT marker because the
  // previous design inferred it from a missing `expected_model` — and absence has a second
  // cause, an agent_view with no model configured, which then silently disabled the guard.
  // Absent marker = enforce, so a connection file written by an older build behaves as before.
  if (raw.allow_model_substitution !== undefined) {
    if (typeof raw.allow_model_substitution !== 'boolean') {
      throw new BridgeError('config.allow_model_substitution must be a boolean when present');
    }
    cfg.allowModelSubstitution = raw.allow_model_substitution;
  }

  return cfg;
}

// ---------------------------------------------------------------------------
// Streamable HTTP transport
// ---------------------------------------------------------------------------

/**
 * Parse one SSE body into the first JSON payload it carries.
 *
 * The Toolbox constructs its transport without `enableJsonResponse`, and the SDK
 * defaults that to false, so POST replies come back as `text/event-stream` — an Accept
 * header does not change it. Frames are separated by a blank line; `data:` may repeat
 * across lines and is joined with newlines; lines starting with `:` are comments.
 */
export function parseSse(body) {
  for (const frame of body.split(/\r?\n\r?\n/)) {
    const data = [];
    for (const line of frame.split(/\r?\n/)) {
      if (line.startsWith(':') || line.trim() === '') continue;
      if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, ''));
    }
    if (data.length === 0) continue;
    try {
      return JSON.parse(data.join('\n'));
    } catch {
      // Not JSON — keep scanning; a later frame may carry the response.
    }
  }
  return undefined;
}

/** Validate a JSON-RPC envelope and return its `result`. */
export function unwrapEnvelope(payload, expectedId) {
  if (payload === null || typeof payload !== 'object' || Array.isArray(payload)) {
    throw new BridgeError('response is not a JSON-RPC object');
  }
  if (payload.jsonrpc !== '2.0') {
    throw new BridgeError(`unexpected jsonrpc version ${JSON.stringify(payload.jsonrpc)}`);
  }
  if (payload.id !== expectedId) {
    throw new BridgeError(`response id ${JSON.stringify(payload.id)} does not match request ${expectedId}`);
  }
  const hasResult = Object.hasOwn(payload, 'result');
  const hasError = Object.hasOwn(payload, 'error');
  if (hasResult === hasError) {
    throw new BridgeError('response must carry exactly one of result/error');
  }
  if (hasError) {
    const err = payload.error;
    const code = err && Number.isInteger(err.code) ? err.code : 'unknown';
    const message = err && typeof err.message === 'string' ? err.message : 'unknown error';
    const failure = new BridgeError(`server error ${code}: ${message}`);
    failure.isRpcError = true;
    throw failure;
  }
  return payload.result;
}

class ToolboxClient {
  constructor(cfg) {
    this.cfg = cfg;
    this.sessionId = null;
    this.protocolVersion = null;
    this.closed = false;
    this._nextId = 1;
  }

  _headers() {
    const headers = {
      'Content-Type': 'application/json',
      Accept: 'application/json, text/event-stream',
      ...this.cfg.headers,
    };
    if (this.sessionId) headers['Mcp-Session-Id'] = this.sessionId;
    // The reference SDK client sends the negotiated version on every later request;
    // storing it without returning it is half a handshake.
    if (this.protocolVersion) headers['MCP-Protocol-Version'] = this.protocolVersion;
    return headers;
  }

  async _post(body, { timeoutMs, signal }) {
    const timer = AbortSignal.timeout(timeoutMs);
    const composed = signal ? AbortSignal.any([signal, timer]) : timer;
    let response;
    try {
      response = await fetch(this.cfg.url, {
        method: 'POST',
        headers: this._headers(),
        body: JSON.stringify(body),
        signal: composed,
      });
    } catch (err) {
      if (timer.aborted) throw new BridgeError(`request timed out after ${timeoutMs}ms`);
      throw new BridgeError(`transport failure: ${err.message}`);
    }
    return response;
  }

  async _readPayload(response) {
    const text = await response.text();
    const contentType = (response.headers.get('content-type') || '').toLowerCase();
    if (contentType.includes('text/event-stream')) return parseSse(text);
    if (!text.trim()) return undefined;
    try {
      return JSON.parse(text);
    } catch {
      throw new BridgeError(`response body is neither SSE nor JSON: ${text.slice(0, 200)}`);
    }
  }

  async request(method, params, { timeoutMs, signal } = {}) {
    const id = this._nextId++;
    const response = await this._post(
      { jsonrpc: '2.0', id, method, ...(params ? { params } : {}) },
      { timeoutMs: timeoutMs ?? this.cfg.timeoutMs, signal },
    );
    if (!response.ok) {
      throw new BridgeError(`${method} failed: HTTP ${response.status}`);
    }
    const payload = await this._readPayload(response);
    if (payload === undefined) throw new BridgeError(`${method} returned an empty body`);
    return unwrapEnvelope(payload, id);
  }

  async notify(method, { timeoutMs } = {}) {
    const response = await this._post(
      { jsonrpc: '2.0', method },
      { timeoutMs: timeoutMs ?? this.cfg.handshakeTimeoutMs },
    );
    if (!response.ok) {
      throw new BridgeError(`${method} failed: HTTP ${response.status}`);
    }
  }

  async initialize() {
    const id = this._nextId++;
    const response = await this._post(
      {
        jsonrpc: '2.0',
        id,
        method: 'initialize',
        params: {
          protocolVersion: PROTOCOL_VERSION,
          clientInfo: { name: 'agento-toolbox-bridge', version: '1.0.0' },
          capabilities: {},
        },
      },
      { timeoutMs: this.cfg.handshakeTimeoutMs },
    );
    if (!response.ok) throw new BridgeError(`initialize failed: HTTP ${response.status}`);

    const sessionId = response.headers.get('mcp-session-id');
    if (!sessionId) {
      // Without it every later request starts a NEW server-side session, multiplying
      // entries in the Toolbox's session map.
      throw new BridgeError('initialize returned no Mcp-Session-Id header');
    }
    this.sessionId = sessionId;

    const result = unwrapEnvelope(await this._readPayload(response), id);
    const version = result && result.protocolVersion;
    if (typeof version !== 'string' || !version) {
      throw new BridgeError('initialize returned no protocolVersion');
    }
    if (!SUPPORTED_PROTOCOL_VERSIONS.has(version)) {
      throw new BridgeError(`unsupported protocolVersion ${JSON.stringify(version)}`);
    }
    this.protocolVersion = version;
    return result;
  }

  async listTools() {
    const tools = [];
    const seenCursors = new Set();
    let cursor;
    for (let page = 0; page < this.cfg.maxToolPages; page += 1) {
      const result = await this.request('tools/list', cursor ? { cursor } : undefined, {
        timeoutMs: this.cfg.handshakeTimeoutMs,
      });
      if (!result || !Array.isArray(result.tools)) {
        throw new BridgeError('tools/list did not return a tools array');
      }
      tools.push(...result.tools);

      const next = result.nextCursor;
      if (next === undefined || next === null) return tools;
      if (typeof next !== 'string' || next === '') {
        throw new BridgeError('tools/list returned a non-string nextCursor');
      }
      // A server repeating a cursor would otherwise spin here forever, inside
      // extension loading, with no output.
      if (seenCursors.has(next)) {
        throw new BridgeError('tools/list repeated a pagination cursor');
      }
      seenCursors.add(next);
      cursor = next;
    }
    throw new BridgeError(`tools/list exceeded ${this.cfg.maxToolPages} pages`);
  }

  /** Best-effort; never throws. Losing the session is not worth failing a finished run. */
  async close() {
    if (this.closed || !this.sessionId) return;
    this.closed = true;
    try {
      await fetch(this.cfg.url, {
        method: 'DELETE',
        headers: this._headers(),
        signal: AbortSignal.timeout(this.cfg.handshakeTimeoutMs),
      });
    } catch {
      // Ignored deliberately.
    }
  }
}

// ---------------------------------------------------------------------------
// Tool mapping
// ---------------------------------------------------------------------------

/**
 * An MCP `inputSchema` must be an object schema. Checking only "is an object" would let
 * `{"type":"array"}` through and blow up `registerTool` inside the factory — which is
 * fatal — instead of skipping one tool.
 */
export function isUsableInputSchema(schema) {
  if (schema === null || typeof schema !== 'object' || Array.isArray(schema)) return false;
  if (schema.type !== 'object') return false;
  if (schema.properties !== undefined) {
    const props = schema.properties;
    if (props === null || typeof props !== 'object' || Array.isArray(props)) return false;
    for (const value of Object.values(props)) {
      if (value === null || typeof value !== 'object' || Array.isArray(value)) return false;
    }
  }
  if (schema.required !== undefined) {
    if (!Array.isArray(schema.required)) return false;
    if (!schema.required.every((entry) => typeof entry === 'string')) return false;
  }
  // Extra keys are fine and in fact present in practice (`execution`): JSON Schema
  // allows them and the reference SDK uses `.catchall`.
  return true;
}

/** Turn one `tools/list` entry into a registrable descriptor, or null with a reason. */
export function normalizeTool(entry) {
  if (entry === null || typeof entry !== 'object' || Array.isArray(entry)) {
    return { skip: 'entry is not an object' };
  }
  const name = entry.name;
  if (typeof name !== 'string' || name === '') return { skip: 'entry has no name' };
  if (!TOOL_NAME_RE.test(name)) return { skip: `name ${JSON.stringify(name)} is not [a-z][a-z0-9_]*` };
  if (name.length > MAX_RAW_NAME) {
    return { skip: `name ${JSON.stringify(name)} exceeds ${MAX_RAW_NAME} chars` };
  }
  if (!isUsableInputSchema(entry.inputSchema)) {
    return { skip: `tool ${name} has an unusable inputSchema` };
  }
  return {
    rawName: name,
    piName: `${TOOL_PREFIX}${name}`,
    description: typeof entry.description === 'string' ? entry.description : '',
    inputSchema: entry.inputSchema,
  };
}

/**
 * Map an MCP `tools/call` result into Pi's shape.
 *
 * Pi signals a failed tool by a THROWN error — "Returning a value never sets the error
 * flag regardless of what properties you include in the return object" (its docs). So a
 * failure returned as data would reach the model as a *successful* result whose text
 * happens to describe a failure.
 */
export function toAgentToolResult(result) {
  if (result === null || typeof result !== 'object' || Array.isArray(result)) {
    throw new BridgeError('tools/call returned a non-object result');
  }
  if (!Array.isArray(result.content)) {
    throw new BridgeError('tools/call result has no content array');
  }

  const parts = [];
  for (const block of result.content) {
    if (block === null || typeof block !== 'object' || Array.isArray(block)) continue;
    if (typeof block.type !== 'string') continue;
    if (block.type === 'text') {
      if (typeof block.text === 'string') parts.push(block.text);
      continue;
    }
    // Pi's extension tools cannot return binary; describe it instead of dropping it.
    parts.push(`[${block.type}]`);
  }
  const text = parts.join('\n');

  // A non-boolean isError is treated as an error: surfacing a failure the model can
  // react to beats silently presenting it as success.
  if (Object.hasOwn(result, 'isError') && result.isError !== false) {
    throw new BridgeError(text || 'tool reported an error');
  }
  return { content: [{ type: 'text', text }], details: {} };
}

// ---------------------------------------------------------------------------
// Extension entry point
// ---------------------------------------------------------------------------

export default async function agentoToolbox(pi) {
  // Read from the factory body, not a CLI flag: flags registered by extensions are
  // applied AFTER the factories run, so the value would not exist yet.
  const configPath = path.resolve(process.cwd(), CONFIG_FILENAME);
  let cfg;
  try {
    cfg = parseConfig(JSON.parse(await readFile(configPath, 'utf8')));
  } catch (err) {
    if (err instanceof BridgeError) throw err;
    throw new BridgeError(`cannot read ${configPath}: ${err.message}`);
  }

  const client = new ToolboxClient(cfg);
  const registered = [];
  const skipped = [];

  // Everything below runs in the FACTORY on purpose. A throw here is fatal (Pi reports
  // "Failed to load extension" and exits 1), which is the guarantee we want: a job must
  // never proceed with zero Toolbox tools and report success. A throw from `session_start`
  // would be swallowed by Pi's extension runner and the run would continue silently.
  try {
    await client.initialize();
    await client.notify('notifications/initialized');
    const entries = await client.listTools();

    // Dedupe against the names WE register, not against Pi's live tool list.
    //
    // `pi.getAllTools()` is an ACTION METHOD and throws during extension loading —
    // `core/extensions/loader.js:135-155` installs throwing stubs for it (alongside
    // appendEntry, sendMessage, setModel, …) and only `registerTool()` is explicitly
    // valid at load time. Calling it here made the extension fail to load outright, so
    // every Pi job exited 1. Found by spike S1; no unit test caught it because the test
    // double implemented getAllTools() as a working function.
    //
    // Nothing is lost: the `mcp__toolbox__` prefix already makes shadowing a Pi built-in
    // impossible, so the only real risk is two identical names inside one `tools/list`,
    // which a local set covers.
    const existing = new Set();

    for (const entry of entries) {
      const tool = normalizeTool(entry);
      if (tool.skip) {
        skipped.push(tool.skip);
        console.error(`[agento-toolbox] skipping tool: ${tool.skip}`);
        continue;
      }
      // Collision is checked on the FINAL name. A Toolbox tool called `bash` becomes
      // `mcp__toolbox__bash` and cannot shadow Pi's built-in `bash`, so rejecting it
      // would discard a legitimate tool.
      if (existing.has(tool.piName)) {
        skipped.push(`duplicate ${tool.piName}`);
        console.error(`[agento-toolbox] skipping duplicate tool ${tool.piName}`);
        continue;
      }
      existing.add(tool.piName);
      registerTool(pi, client, cfg, tool);
      registered.push(tool.piName);
    }
  } catch (err) {
    // The session may already exist server-side; releasing it here is the only chance,
    // because `session_shutdown` never runs when the factory throws.
    await client.close();
    throw err;
  }

  pi.on('session_start', async () => {
    // `pi.appendEntry(customType, data)` is the pinned API (ExtensionAPI in
    // core/extensions/types.d.ts). It surfaces on stdout as
    // {"type":"entry_appended","entry":{"type":"custom","customType":…,"data":…}} and is
    // stored in the session JSONL as that custom entry directly.
    //
    // Failures here are harmless — this is telemetry, not a capability — and Pi's
    // extension runner swallows handler throws anyway, which is exactly why the
    // capability-critical handshake lives in the factory instead.
    try {
      pi.appendEntry(INIT_RECORD, { status: 'connected', tools: registered, skipped });
    } catch (err) {
      console.error(`[agento-toolbox] could not record init: ${err.message}`);
    }
  });

  pi.on('message_end', async (event, ctx) => {
    // Model guard. Pi resolves an unmatched model by SILENT substring matching, so a
    // typo can run a different model and report success. `process.exitCode` survives a
    // clean finish (Pi only overwrites it when print mode itself fails), which makes
    // this a real failure for scripts and CI — a thrown error would just be swallowed.
    // A router may substitute the model but never the provider, so the provider half of
    // the guard stays live even when substitution is allowed.
    const enforceModel = cfg.expectedModel && !cfg.allowModelSubstitution;
    if (!enforceModel && !cfg.expectedProvider) return;
    const message = event?.message;
    if (!message || message.role !== 'assistant') return;
    // A MISSING actual field is a mismatch, not a pass. Requiring `message.model` to be
    // truthy before comparing meant an assistant message that reported no identity sailed
    // through — the very shape a silent substitution can take.
    const wrongModel = enforceModel ? !sameModel(message.model, cfg.expectedModel) : false;
    const wrongProvider = cfg.expectedProvider
      ? message.provider !== cfg.expectedProvider
      : false;
    if (!wrongModel && !wrongProvider) return;

    const detail = {
      expectedProvider: cfg.expectedProvider ?? null,
      expectedModel: cfg.expectedModel ?? null,
      actualProvider: message.provider ?? null,
      actualModel: message.model ?? null,
    };
    console.error(
      `[agento-toolbox] MODEL MISMATCH: ran ${detail.actualProvider}/${detail.actualModel}, ` +
        `expected ${detail.expectedProvider}/${detail.expectedModel}`,
    );
    try {
      pi.appendEntry(MISMATCH_RECORD, detail);
    } catch {
      // telemetry only
    }
    // `ctx.hasUI` is the pinned API (ExtensionContext.hasUI). Assigning
    // `process.exitCode` survives a clean finish — Pi only overwrites it when print mode
    // itself returns non-zero — so a wrong-model headless run fails for scripts and CI
    // instead of exiting 0. A throw here would simply be swallowed.
    if (ctx?.hasUI !== true) {
      process.exitCode = 1;
    }
  });

  pi.on('session_shutdown', async () => {
    await client.close();
  });
}

function registerTool(pi, client, cfg, tool) {
  pi.registerTool({
    name: tool.piName,
    label: tool.rawName,
    description: tool.description,
    parameters: tool.inputSchema,
    async execute(_toolCallId, params, signal) {
      let result;
      try {
        result = await client.request(
          'tools/call',
          { name: tool.rawName, arguments: params ?? {} },
          { timeoutMs: cfg.timeoutMs, signal },
        );
      } catch (err) {
        // Sanitised: this text reaches the model and the transcript, so it must not
        // carry the Toolbox URL, headers or the session id.
        throw new Error(`${tool.rawName} failed: ${sanitize(err.message, cfg)}`);
      }
      return toAgentToolResult(result);
    },
  });
}

/**
 * Compare model ids, tolerating Pi's `~` alias marker.
 *
 * Found by spike S2 against the live API: requesting `anthropic/claude-haiku-latest` makes
 * Pi report `~anthropic/claude-haiku-latest` — the leading `~` marks a catalogue alias. A
 * strict comparison failed a legitimate run. Only the marker is normalised, so a real
 * substitution is still a mismatch.
 */
export function sameModel(actual, wanted) {
  if (typeof actual !== 'string' || typeof wanted !== 'string') return false;
  return actual.replace(/^~+/, '') === wanted.replace(/^~+/, '');
}

export function sanitize(message, cfg) {
  let out = String(message ?? '');
  if (cfg?.url) out = out.split(cfg.url).join('<toolbox>');
  return out.replace(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/gi, '<session>');
}
