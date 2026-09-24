import { randomUUID } from 'node:crypto';
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { SSEServerTransport } from '@modelcontextprotocol/sdk/server/sse.js';
import { StreamableHTTPServerTransport } from '@modelcontextprotocol/sdk/server/streamableHttp.js';
import express from 'express';
import {
  registerTools, registerModuleRestApis, loadScopedDbOverridesStrict, loadWorkspaceOverridesStrict,
  discoverAuthSources, ScopeResolutionError,
} from './config-loader.js';
import {
  requireCapability, tokenHash, extractToken, rejectScopeMismatch, reverifyCapability,
  installAuthSources, createSourceLookup,
} from './capability.js';
import { installToolDispatch, createAuditStore, createConsumer } from './dispatcher.js';
import { installInvokeRoute } from './invoke-route.js';
import { runHealthchecks } from './health-run.js';
import { SqlPoolRegistry } from './adapters/sql-pool-registry.js';
import { createHealthRegistration } from './health-registration.js';
import { McpSessionRegistry, DEFAULT_IDLE_MS, DEFAULT_SWEEP_MS } from './mcp-sessions.js';
import { logToolboxMcp, logToolboxRest, logPublisher, createScopedLogger, createPhasedLogger, errorCategory } from './log.js';
import {
  ProbeLimiter, parseConfigTestRequest, registerConfigTests, runConfigTest,
} from './config-tests.js';
import * as db from './db.js';
import * as playwright from './playwright-client.js';
import { buildArtifactsDir, FALLBACK_ARTIFACTS_DIR } from './artifacts-dir.js';

const PORT = process.env.PORT || 3001;

const app = express();
// NOTE: no express.json() here — SSEServerTransport reads raw body from req stream

const sessions = new Map();
const sqlPoolRegistry = new SqlPoolRegistry({ log: logToolboxRest });

// Shared context passed to all module register() functions.
// Base log is the REST/lifecycle logger: it feeds registerModuleRestApis' REST
// route handlers, startup logging, and the agent-view-less /health probe. MCP
// sessions override context.log with the MCP logger in createServer().
const context = {
  app,
  log: logToolboxRest,
  logPublisher,
  db,
  sqlPoolRegistry,
  playwright: {
    getClient: playwright.getPlaywrightClient,
    getTools: playwright.getPlaywrightTools,
    getState: playwright.getPlaywrightState,
    getViewport: playwright.getPlaywrightViewport,
  },
};

// Registers the tools a capability's scope enables on `server` and returns the dispatcher's
// registry. Shared by MCP sessions and invoke, so both run the same handlers with the same
// offload path.
async function buildRegistration(server, authContext, runId = null) {
  const { agent_view_id: agentViewId, job_id: jobId } = authContext;
  let artifactsDir = FALLBACK_ARTIFACTS_DIR;
  // jobId (from the capability row, null for interactive runs / tool-list) flows to every tool's
  // register() via registerTools -> enrichedContext; schedule_followup uses it to inherit the
  // current job's channel/reference/scope.
  // invocationLog is the MCP tool-invocation logger for this session: logToolboxMcp for
  // interactive/tool-list runs, or the agent_view-scoped variant when an agent_view is known.
  let invocationLog = logToolboxMcp;
  let sessionContext = { ...context, artifactsDir, jobId };
  let preloadedOverrides;
  if (agentViewId) {
    const { overrides, agentViewMeta } = await loadScopedDbOverridesStrict(agentViewId);
    preloadedOverrides = overrides;
    if (agentViewMeta) {
      artifactsDir = buildArtifactsDir(agentViewMeta, jobId, runId);
      invocationLog = createScopedLogger(agentViewMeta);
      sessionContext = { ...sessionContext, artifactsDir };
    }
  } else {
    preloadedOverrides = await loadWorkspaceOverridesStrict(authContext.workspace_id);
  }

  // Registration-time diagnostics that modules emit from register() (e.g. browser SESSION/INIT)
  // are lifecycle noise and must stay out of toolbox_mcp.log. The phased logger routes them to
  // toolbox_rest.log, then flips to invocationLog once registration completes — before any tool
  // handler can run — so only real invocations reach toolbox_mcp.log.
  const sessionLog = createPhasedLogger(invocationLog);
  sessionContext = { ...sessionContext, log: sessionLog };
  const registration = await registerTools(server, sessionContext, agentViewId, preloadedOverrides);
  sessionLog.toInvocationPhase();
  return registration;
}

const dbQuery = (sql, params) => db.getCronPool().query(sql, params);
const dispatchDeps = {
  audit: createAuditStore(dbQuery),
  consume: createConsumer(dbQuery),
  reverify: reverifyCapability,
  // Enablement is re-read on every call, strictly: a disabled tool is refused at its next call.
  loadOverrides: async (ctx) => (ctx.agent_view_id
    ? (await loadScopedDbOverridesStrict(ctx.agent_view_id)).overrides
    : loadWorkspaceOverridesStrict(ctx.workspace_id)),
  log: logToolboxRest,
};

// `endpoint` is where the session's tool calls arrive: `messages` for SSE, `mcp` for
// Streamable HTTP. Each call is re-verified there.
async function createServer(authContext, endpoint, runId = null) {
  const server = new McpServer({
    name: 'toolbox',
    version: '1.0.0',
  });
  const registration = await buildRegistration(server, authContext, runId);
  installToolDispatch(server, registration, authContext, { ...dispatchDeps, endpoint });
  return { server, healthchecks: registration.healthchecks };
}

// `run_id` names an interactive run's desk. The capability row has no run id, so it
// comes from the query string — it NAMES a directory, it grants no scope: the view and
// the job still come only from the capability. A `job_id` in the query (the harness
// URL still carries one for a job run) may only AGREE with the capability's job.
function runIdFrom(req) {
  return req.capability.job_id === null && req.query.run_id ? String(req.query.run_id) : null;
}

function rejectJobMismatch(req, res) {
  const supplied = req.query.job_id;
  if (supplied === undefined || String(supplied) === String(req.capability.job_id)) return false;
  logToolboxRest('auth', 'ERROR', 'supplied job_id disagrees with the capability');
  res.status(400).json({ error: 'job_id does not match the capability' });
  return true;
}


// SSE is verified ONCE, at connect: a long-lived stream has no per-request hook, so a token
// revoked mid-stream is only enforced at the next connect. /mcp re-verifies every request.
app.get('/sse', requireCapability({ endpoint: 'sse' }, logToolboxRest), async (req, res) => {
  if (rejectScopeMismatch(req, res, logToolboxRest, 'sse') || rejectJobMismatch(req, res)) return undefined;
  let server;
  try {
    ({ server } = await createServer(req.capability, 'messages', runIdFrom(req)));
  } catch (err) {
    return sendScopeError(res, err);
  }
  // The endpoint the SDK advertises carries the connect capability. An MCP client configures a
  // bare URL and sends NO headers on the `/messages` POST — it posts to this string verbatim —
  // so without `cap` here the guarded `/messages` would answer 401 to every legitimate client.
  // The SDK appends `sessionId` with URLSearchParams, which keeps `cap` intact.
  const connectToken = extractToken(req);
  const transport = new SSEServerTransport(
    `/messages?cap=${encodeURIComponent(connectToken)}`,
    res
  );
  // The session is owned by the capability that opened it. `/messages` carries the sessionId in a
  // QUERY STRING — a value that lands in access logs — so the id alone must never authorize a post.
  sessions.set(transport.sessionId, { transport, capabilityHash: tokenHash(connectToken) });

  res.on('close', () => {
    sessions.delete(transport.sessionId);
    server.close().catch(() => {});
  });

  await server.connect(transport);
});

// A capability that names a view the toolbox cannot resolve is a 403 (the scope is gone);
// a resolver failure is a 503 (transient). Neither may fall back to global config.
function sendScopeError(res, err) {
  if (err instanceof ScopeResolutionError) {
    logToolboxRest('auth', 'ERROR', `capability scope unresolvable: ${err.message}`);
    return res.status(403).json({ error: 'capability scope unresolvable' });
  }
  logToolboxRest('auth', 'ERROR', `scope resolution failed: ${errorCategory(err)}`);
  return res.status(503).json({ error: 'scope resolution unavailable' });
}

// The SSE stream is one half of the transport; THIS is the half that carries the tool calls.
// It is guarded exactly like `/mcp`: a valid MCP capability, and the one that opened the session.
app.post('/messages', requireCapability({ endpoint: 'messages' }, logToolboxRest), async (req, res) => {
  const sessionId = req.query.sessionId;
  const entry = sessions.get(sessionId);
  if (!entry) {
    return res.status(400).json({ error: 'Unknown session' });
  }
  if (entry.capabilityHash !== tokenHash(extractToken(req))) {
    logToolboxRest('auth', 'ERROR', `capability does not own sse session ${sessionId}`);
    return res.status(403).json({ error: 'capability does not own this session' });
  }
  return entry.transport.handlePostMessage(req, res);
});

// Streamable HTTP transport (used by Codex and newer MCP clients)
// Stateful: reuse server+transport per session to avoid re-registering tools on every
// request. The registry carries an idle TTL because a SIGKILLed agent never sends DELETE
// — see mcp-sessions.js for why that has to be fixed server-side.
const MCP_SESSION_IDLE_MS =
  parseInt(process.env.MCP_SESSION_IDLE_MS || '', 10) || DEFAULT_IDLE_MS;
const MCP_SESSION_SWEEP_MS =
  parseInt(process.env.MCP_SESSION_SWEEP_MS || '', 10) || DEFAULT_SWEEP_MS;

const mcpSessions = new McpSessionRegistry({
  idleMs: MCP_SESSION_IDLE_MS,
  logger: (message) => console.log(message),
});
mcpSessions.startSweeper(MCP_SESSION_SWEEP_MS);

// The guard runs BEFORE the session short-circuit, so every Streamable-HTTP request
// re-verifies against the DB and a revoked token dies at its very next request.
app.all('/mcp', requireCapability({ endpoint: 'mcp' }, logToolboxRest), async (req, res) => {
  const sessionId = req.headers['mcp-session-id'];
  if (sessionId && mcpSessions.has(sessionId)) {
    const entry = mcpSessions.get(sessionId);
    // A live session is owned by the capability that created it. A different
    // capability may not drive it, even if that capability is itself valid.
    if (entry.capabilityHash !== tokenHash(extractToken(req))) {
      logToolboxRest('auth', 'ERROR', `capability does not own mcp session ${sessionId}`);
      return res.status(403).json({ error: 'capability does not own this session' });
    }
    mcpSessions.touch(sessionId);
    await entry.transport.handleRequest(req, res, req.body);
    return;
  }

  if (rejectScopeMismatch(req, res, logToolboxRest, 'mcp') || rejectJobMismatch(req, res)) return undefined;
  const transport = new StreamableHTTPServerTransport({
    sessionIdGenerator: () => randomUUID(),
  });
  let server;
  try {
    ({ server } = await createServer(req.capability, 'mcp', runIdFrom(req)));
  } catch (err) {
    return sendScopeError(res, err);
  }

  let closing = false;
  transport.onclose = () => {
    if (closing) return;
    closing = true;
    if (transport.sessionId) {
      mcpSessions.delete(transport.sessionId);
    }
    server.close().catch(() => {});
  };

  await server.connect(transport);
  await transport.handleRequest(req, res, req.body);

  if (transport.sessionId) {
    mcpSessions.set(transport.sessionId, {
      transport,
      server,
      capabilityHash: tokenHash(extractToken(req)),
    });
  }
});

// POST /internal/tools/{name}:invoke — see invoke-route.js. The registry is built per request,
// inside executeTool, after the pending audit row.
installInvokeRoute(app, {
  guard: requireCapability({ endpoint: 'invoke' }, logToolboxRest),
  deps: dispatchDeps,
  loadRegistryFor: authContext => () => buildRegistration({ tool: () => {} }, authContext),
  log: logToolboxRest,
});

let configTestRegistry = new Map();

// POST /config-test?path=<module>/<field>[&agent_view_id=N]
//
// POST because this triggers a live login attempt at a third party. Like scoped
// `/health`, it is an operator action, so it needs an `internal_rest` capability
// and takes its scope from THAT row, never from the query: `?agent_view_id=` may
// only agree with it. It is the one route that also accepts a VIEWLESS
// capability, which tests the default scope — every other guard refuses one.
//
// A caller could also hammer a real credential and trip an
// account lockout, so one CREDENTIAL may be probed at most once every
// CONFIG_TEST_COOLDOWN_MS. Keyed per credential (declaration group + scope), not
// per config path — six field paths reaching one Graph token must share one
// budget — and not per caller, because there is no caller identity to key on and
// the resource being protected is the remote account. `runConfigTest` applies it
// at the last line before the probe call — after every verdict it reaches on its
// own — so a static fault (a duplicate probe name, unreadable config, an empty
// field) keeps its own diagnosis on a retry instead of being masked by COOLDOWN.
// See `ProbeLimiter`.
//
// Always HTTP 200 with a four-state body: the FRAMEWORK decides how to render
// "could not check" versus "credential rejected", and an HTTP error code would
// collapse that distinction into the transport layer.
const CONFIG_TEST_COOLDOWN_MS = 3_000;
// Constructed once at module load and never rebound per session — bounded
// internally, like `sessions` above and `mcpSessions` below.
const configTestLimiter = new ProbeLimiter({ cooldownMs: CONFIG_TEST_COOLDOWN_MS });

app.post('/config-test', requireCapability({ endpoint: 'config_test' }, logToolboxRest), async (req, res) => {
  if (rejectScopeMismatch(req, res, logToolboxRest, 'config-test')) return undefined;
  const parsed = parseConfigTestRequest(req.query);
  if (parsed.error) return res.json({ ...parsed.error, path: parsed.path });
  const { configPath } = parsed;
  const agentViewId = req.capability.agent_view_id;
  const result = await runConfigTest(
    { path: configPath, agentViewId },
    { namedTests: configTestRegistry, limiter: configTestLimiter },
  );
  logToolboxRest('config-test', result.status === 'ok' ? 'OK' : 'ERROR',
    `${configPath} -> ${result.status} [${result.code}]`);
  res.json(result);
});

// Scoped diagnostics are an operator/publisher action, so ONLY internal_rest qualifies.
// An mcp_job token is held by the sandboxed agent; letting it drive ?test=true would let the
// agent fire every scoped, secret-backed healthcheck at will.
const healthGuard = requireCapability({ endpoint: 'health' }, logToolboxRest);

async function scopedHealth(req, res) {
  // `?agent_view_id=` no longer selects the scope (the capability row does), so a value that
  // disagrees is a 400 rather than a silently different answer. `?test=true` is the documented
  // way to ask for the scoped diagnostic.
  if (rejectScopeMismatch(req, res, logToolboxRest, 'health')) return undefined;
  const agentViewId = req.capability.agent_view_id;
  let registration;
  try {
    registration = await createHealthRegistration(agentViewId, context, { strict: true });
  } catch (err) {
    return sendScopeError(res, err);
  }
  const { tools, healthchecks } = registration;
  const runTests = req.query.test === 'true';
  const response = { status: 'ok', tools, playwright: playwright.getPlaywrightState(), agent_view_id: agentViewId };
  if (runTests) {
    response.checks = await runHealthchecks(healthchecks);
    response.status = response.checks.some(c => c.status === 'fail') ? 'degraded' : 'ok';
  }
  return res.json(response);
}

app.get('/health', async (req, res) => {
  // Bare /health stays unauthenticated: Docker's HEALTHCHECK needs it, and it exposes only
  // tool names and Playwright state. It cares about the HTTP status code only — a dead
  // Playwright subsystem leaves the body status=degraded but HTTP 200, so the container
  // stays (healthy) and other adapters keep serving.
  const wantsScope = req.query.agent_view_id !== undefined || req.query.test === 'true';
  if (!wantsScope) {
    const { tools } = await createHealthRegistration(null, context);
    return res.json({ status: 'ok', tools, playwright: playwright.getPlaywrightState() });
  }
  return healthGuard(req, res, () => scopedHealth(req, res));
});

// EVERY module REST route lives under /api and is scoped by a capability. Guarding the
// namespace (rather than each route) means a new module inherits authentication with no
// opt-in, and cannot forget it. Handlers read req.capability; a caller-supplied
// agent_view_id in a body may only MATCH it, never override it.
app.use('/api', requireCapability({ endpoint: 'api' }, logToolboxRest));

// Register module REST APIs and start Playwright in parallel, then listen
Promise.allSettled([
  registerModuleRestApis(context)
    .then(() => logToolboxRest('startup', 'OK', 'Module REST APIs registered')),
  registerConfigTests()
    .then((registry) => {
      configTestRegistry = registry;
      logToolboxRest('startup', 'OK', `Registered ${registry.size} config test(s)`);
    }),
  playwright.initPlaywright(),
  // Discovery is the only writer of the auth-source lookup, and it runs once. Until it
  // completes the lookup answers nothing, so every user_session/miniapp row is refused.
  discoverAuthSources()
    .then((entries) => {
      installAuthSources(createSourceLookup(entries));
      logToolboxRest('startup', 'OK', `Registered ${entries.length} auth source(s)`);
    }),
]).then(([restResult, configTestResult, playwrightResult, authSourcesResult]) => {
  if (authSourcesResult.status === 'rejected') {
    logToolboxRest('startup', 'ERROR', `Auth source discovery failed: ${errorCategory(authSourcesResult.reason)}`);
  }
  if (restResult.status === 'rejected') {
    logToolboxRest('startup', 'ERROR', `Module REST API registration failed: ${restResult.reason?.message}`);
  }
  if (configTestResult.status === 'rejected') {
    // The registry stays an empty Map, so a declared named tester answers
    // UNKNOWN_TESTER instead of the route disappearing.
    logToolboxRest('startup', 'ERROR', `Config test registration failed: ${configTestResult.reason?.message}`);
  }
  if (playwrightResult.status === 'rejected') {
    logToolboxRest('playwright', 'ERROR', `Failed to start Playwright MCP: ${playwrightResult.reason?.message}. Auto-restart loop will retry up to MAX_ATTEMPTS.`);
  }
  app.listen(PORT, '0.0.0.0', () => {
    console.log(`Toolbox MCP server listening on port ${PORT}`);
  });
});

// Graceful shutdown
process.on('SIGTERM', async () => {
  await sqlPoolRegistry.closeAll();
  await playwright.closePlaywright();
  process.exit(0);
});
