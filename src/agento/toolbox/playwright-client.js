import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport, getDefaultEnvironment } from '@modelcontextprotocol/sdk/client/stdio.js';
import { readFile, writeFile } from 'node:fs/promises';
import { logToolboxRest as log } from './log.js';

// SSE/HTTP sessions cache the upstream tool list at registration time, so after
// a Playwright MCP child crash + reconnect, *existing* sessions keep the old
// passthrough list until they close. Sessions are short-lived (per request),
// so new sessions pick up the refreshed `discoveredTools` immediately.
let client = null;
let transport = null;
let discoveredTools = [];

const MAX_ATTEMPTS = 5;
const BACKOFF_MS = [1000, 2000, 4000, 8000, 16000];
const STABILITY_RESET_MS = 30000;

// Playwright's default (viewport: null) renders the page at the unpredictable
// headless window size and makes on-demand video fall back to a tiny 800x600
// canvas, producing low-res, letterboxed recordings. We always emulate a fixed
// viewport so screenshots and video are crisp and full-frame. session.viewport
// overrides this default. This is also the single source of truth for the
// default video size (see browser_start_video in browser.js).
const DEFAULT_VIEWPORT = { width: 1280, height: 720 };
let resolvedViewport = { ...DEFAULT_VIEWPORT };

let shuttingDown = false;
let initInFlight = null;
let stabilityTimer = null;

const state = {
  state: 'starting',
  attempt: 0,
  maxAttempts: MAX_ATTEMPTS,
  lastError: null,
};

async function buildMcpArgs() {
  const args = ['@playwright/mcp', '--headless', '--browser', 'chromium', '--ignore-https-errors', '--caps', 'devtools'];

  let session = {};
  try {
    const raw = await readFile('/app/session.json', 'utf8');
    session = JSON.parse(raw);
    log('playwright', 'SESSION', 'Loaded session.json');
  } catch {
    // No session.json — proceed with defaults
  }

  resolvedViewport = session.viewport || { ...DEFAULT_VIEWPORT };
  args.push('--viewport-size', `${resolvedViewport.width},${resolvedViewport.height}`);
  if (session.userAgent) {
    args.push('--user-agent', session.userAgent);
  }
  if (session.cookies?.length) {
    const storageState = { cookies: session.cookies, origins: [] };
    await writeFile('/tmp/pw-storage.json', JSON.stringify(storageState));
    args.push('--storage-state', '/tmp/pw-storage.json');
  }

  return args;
}

async function connectClient() {
  // The MCP SDK forwards only its curated allowlist (getDefaultEnvironment) to
  // the spawned child — PLAYWRIGHT_BROWSERS_PATH is NOT in it. The toolbox image
  // installs Chromium to /opt/playwright via that var, so the @playwright/mcp
  // child must receive it explicitly; otherwise it looks in the empty default
  // $HOME/.cache/ms-playwright and every browser tool fails with
  // 'Browser "chromium" is not installed'. Forward ONLY the non-secret
  // Playwright var on top of the default env — never the full toolbox env, so
  // the secrets this container holds never reach the browser child.
  const childEnv = { ...getDefaultEnvironment() };
  if (process.env.PLAYWRIGHT_BROWSERS_PATH) {
    childEnv.PLAYWRIGHT_BROWSERS_PATH = process.env.PLAYWRIGHT_BROWSERS_PATH;
  }

  transport = new StdioClientTransport({
    command: 'npx',
    args: await buildMcpArgs(),
    stderr: 'pipe',
    env: childEnv,
  });

  transport.stderr?.on('data', (chunk) => {
    const msg = chunk.toString().trim();
    if (msg) log('playwright-mcp', 'STDERR', msg);
  });

  const newClient = new Client(
    { name: 'toolbox-playwright', version: '1.0.0' },
    { capabilities: {} },
  );

  await newClient.connect(transport);
  log('playwright', 'INIT', 'Playwright MCP child process started');

  try {
    const { tools } = await newClient.listTools();
    discoveredTools = tools;
    const names = tools.map(t => t.name);
    log('playwright', 'TOOLS', `${names.length} available: ${names.join(', ')}`);
  } catch (err) {
    log('playwright', 'WARN', `Failed to list tools: ${err.message}`);
  }

  newClient.onclose = handleClose;
  client = newClient;
}

function handleClose() {
  log('playwright', 'CLOSE', 'Playwright MCP child process closed');
  client = null;
  // connectClient() only sets lastError when *connect* throws. When the child
  // boots fine and dies later (Chromium SIGSEGV, OOM, …), onclose fires with no
  // connect-level error in scope — leave the marker so the eventual FATAL log
  // and the agent-facing "permanently failed" message carry a meaningful cause
  // instead of "unknown".
  if (!state.lastError) {
    state.lastError = 'Playwright child process closed unexpectedly';
  }
  if (stabilityTimer) {
    clearTimeout(stabilityTimer);
    stabilityTimer = null;
  }
  if (shuttingDown) return;
  scheduleRestart();
}

function scheduleRestart() {
  if (state.attempt >= MAX_ATTEMPTS) {
    state.state = 'failed';
    log('playwright', 'FATAL', `Playwright MCP permanently failed after ${state.attempt} attempts. Last error: ${state.lastError || 'unknown'}. Browser tools will return permanent-failure errors until toolbox is restarted.`);
    return;
  }
  state.attempt += 1;
  state.state = 'restarting';
  const backoff = BACKOFF_MS[Math.min(state.attempt - 1, BACKOFF_MS.length - 1)];
  log('playwright', 'RESTART', `attempt ${state.attempt}/${MAX_ATTEMPTS} backoff=${backoff}ms`);
  setTimeout(() => {
    if (shuttingDown) return;
    initPlaywright().catch(() => { /* connect error already logged + rescheduled inside */ });
  }, backoff);
}

export async function initPlaywright() {
  if (shuttingDown) return null;
  if (initInFlight) return initInFlight;
  initInFlight = (async () => {
    try {
      await connectClient();
      state.state = 'ready';
      state.lastError = null;
      if (stabilityTimer) clearTimeout(stabilityTimer);
      stabilityTimer = setTimeout(() => {
        if (state.state === 'ready') state.attempt = 0;
        stabilityTimer = null;
      }, STABILITY_RESET_MS);
      return client;
    } catch (err) {
      state.lastError = err?.message || String(err);
      log('playwright', 'ERROR', `Failed to connect Playwright MCP: ${state.lastError}`);
      scheduleRestart();
      throw err;
    } finally {
      initInFlight = null;
    }
  })();
  return initInFlight;
}

export function getPlaywrightState() {
  return { ...state };
}

export function getPlaywrightClient() {
  return client;
}

export function getPlaywrightTools() {
  return discoveredTools;
}

export function getPlaywrightViewport() {
  return { ...resolvedViewport };
}

export async function closePlaywright() {
  shuttingDown = true;
  if (stabilityTimer) {
    clearTimeout(stabilityTimer);
    stabilityTimer = null;
  }
  if (client) {
    await client.close().catch(() => {});
    client = null;
  }
}

// Test-only: reset internal state between vitest test cases.
export function __resetForTests() {
  client = null;
  transport = null;
  discoveredTools = [];
  resolvedViewport = { ...DEFAULT_VIEWPORT };
  shuttingDown = false;
  initInFlight = null;
  if (stabilityTimer) clearTimeout(stabilityTimer);
  stabilityTimer = null;
  state.state = 'starting';
  state.attempt = 0;
  state.lastError = null;
}
