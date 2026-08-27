import { randomUUID } from 'node:crypto';
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { SSEServerTransport } from '@modelcontextprotocol/sdk/server/sse.js';
import { StreamableHTTPServerTransport } from '@modelcontextprotocol/sdk/server/streamableHttp.js';
import express from 'express';
import { registerTools, registerModuleRestApis, loadScopedDbOverrides } from './config-loader.js';
import { SqlPoolRegistry } from './adapters/sql-pool-registry.js';
import { createHealthRegistration } from './health-registration.js';
import { McpSessionRegistry, DEFAULT_IDLE_MS, DEFAULT_SWEEP_MS } from './mcp-sessions.js';
import { logToolboxMcp, logToolboxRest, logPublisher, createScopedLogger, createPhasedLogger } from './log.js';
import { runHealthchecks } from './healthchecks.js';
import {
  ProbeLimiter, parseConfigTestRequest, registerConfigTests, runConfigTest,
} from './config-tests.js';
import * as db from './db.js';
import * as playwright from './playwright-client.js';

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

function buildArtifactsDir(agentViewMeta, jobId) {
  if (!agentViewMeta || !jobId) return '/workspace/artifacts/_fallback';
  const safeWs = String(agentViewMeta.workspaceCode || '').replace(/[^a-zA-Z0-9_-]/g, '');
  const safeAv = String(agentViewMeta.agentViewCode || '').replace(/[^a-zA-Z0-9_-]/g, '');
  const safeJobId = String(jobId).replace(/[^0-9]/g, '');
  if (safeWs && safeAv && safeJobId) {
    return `/workspace/artifacts/${safeWs}/${safeAv}/${safeJobId}`;
  }
  return '/workspace/artifacts/_fallback';
}

async function createServer(agentViewId = null, jobId = null) {
  const server = new McpServer({
    name: 'toolbox',
    version: '1.0.0',
  });

  // Build scoped context with agent_view-aware logger before registering tools,
  // so adapters use the scoped log from the start.
  let artifactsDir = '/workspace/artifacts/_fallback';
  // jobId (from req.query.job_id, null for interactive runs / tool-list) flows to every tool's
  // register() via registerTools -> enrichedContext; schedule_followup uses it to inherit the
  // current job's channel/reference/scope.
  // invocationLog is the MCP tool-invocation logger for this session: logToolboxMcp for
  // interactive/tool-list runs, or the agent_view-scoped variant when an agent_view is known.
  let invocationLog = logToolboxMcp;
  let sessionContext = { ...context, artifactsDir, jobId };
  let preloadedOverrides = null;
  if (agentViewId) {
    const { overrides, agentViewMeta } = await loadScopedDbOverrides(agentViewId);
    preloadedOverrides = overrides;
    if (agentViewMeta) {
      artifactsDir = buildArtifactsDir(agentViewMeta, jobId);
      invocationLog = createScopedLogger(agentViewMeta);
      sessionContext = { ...sessionContext, artifactsDir };
    }
  }

  // Registration-time diagnostics that modules emit from register() (e.g. browser SESSION/INIT)
  // are lifecycle noise and must stay out of toolbox_mcp.log. The phased logger routes them to
  // toolbox_rest.log, then flips to invocationLog once registration completes — before any tool
  // handler can run — so only real invocations reach toolbox_mcp.log.
  const sessionLog = createPhasedLogger(invocationLog);
  sessionContext = { ...sessionContext, log: sessionLog };
  const { healthchecks } = await registerTools(server, sessionContext, agentViewId, preloadedOverrides);
  sessionLog.toInvocationPhase();
  return { server, healthchecks };
}

app.get('/sse', async (req, res) => {
  const agentViewId = req.query.agent_view_id ? parseInt(req.query.agent_view_id, 10) : null;
  const jobId = req.query.job_id ? parseInt(req.query.job_id, 10) : null;
  const transport = new SSEServerTransport('/messages', res);
  sessions.set(transport.sessionId, transport);

  const { server } = await createServer(agentViewId, jobId);

  res.on('close', () => {
    sessions.delete(transport.sessionId);
    server.close().catch(() => {});
  });

  await server.connect(transport);
});

app.post('/messages', async (req, res) => {
  const sessionId = req.query.sessionId;
  const transport = sessions.get(sessionId);
  if (transport) {
    await transport.handlePostMessage(req, res);
  } else {
    res.status(400).json({ error: 'Unknown session' });
  }
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

app.all('/mcp', async (req, res) => {
  const sessionId = req.headers['mcp-session-id'];
  if (sessionId && mcpSessions.has(sessionId)) {
    const { transport } = mcpSessions.get(sessionId);
    mcpSessions.touch(sessionId);
    await transport.handleRequest(req, res, req.body);
    return;
  }

  const agentViewId = req.query.agent_view_id ? parseInt(req.query.agent_view_id, 10) : null;
  const jobId = req.query.job_id ? parseInt(req.query.job_id, 10) : null;
  const transport = new StreamableHTTPServerTransport({
    sessionIdGenerator: () => randomUUID(),
  });
  const { server } = await createServer(agentViewId, jobId);

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
    mcpSessions.set(transport.sessionId, { transport, server, lastSeen: Date.now() });
  }
});

let configTestRegistry = new Map();

// POST /config-test?path=<module>/<field>[&agent_view_id=N]
//
// POST because this triggers a live login attempt at a third party. The toolbox
// authenticates no caller — the internal Docker network IS its boundary, exactly
// as for `/sse`, `/mcp` and `/health`, all of which already take an
// `agent_view_id` from the caller and hand back that view's credentials. Adding
// a check here alone would buy nothing while the MCP routes stay open.
//
// What IS new is that a caller could hammer a real credential and trip an
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

app.post('/config-test', async (req, res) => {
  const parsed = parseConfigTestRequest(req.query);
  if (parsed.error) return res.json({ ...parsed.error, path: parsed.path });
  const { configPath, agentViewId } = parsed;
  const result = await runConfigTest(
    { path: configPath, agentViewId },
    { namedTests: configTestRegistry, limiter: configTestLimiter },
  );
  logToolboxRest('config-test', result.status === 'ok' ? 'OK' : 'ERROR',
    `${configPath} -> ${result.status} [${result.code}]`);
  res.json(result);
});

app.get('/health', async (req, res) => {
  const agentViewId = req.query.agent_view_id ? parseInt(req.query.agent_view_id, 10) : null;
  const runTests = req.query.test === 'true';

  const { tools, healthchecks, obscureValues } = await createHealthRegistration(agentViewId, context);

  // Docker HEALTHCHECK uses this endpoint to decide container liveness — it cares
  // about the HTTP status code only. A dead Playwright subsystem leaves the body
  // status=degraded but HTTP 200, so the container stays (healthy) and other
  // adapters keep serving.
  if (!runTests) {
    const response = { status: 'ok', tools, playwright: playwright.getPlaywrightState() };
    if (agentViewId) response.agent_view_id = agentViewId;
    return res.json(response);
  }

  const checks = await runHealthchecks(healthchecks, obscureValues);
  const hasFail = checks.some(c => c.status === 'fail');
  const response = {
    status: hasFail ? 'degraded' : 'ok',
    tools,
    checks,
    playwright: playwright.getPlaywrightState(),
  };
  if (agentViewId) response.agent_view_id = agentViewId;
  res.json(response);
});

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
]).then(([restResult, configTestResult, playwrightResult]) => {
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
