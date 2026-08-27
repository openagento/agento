import { describe, it, expect, vi } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// Static imports: vite cannot resolve a computed specifier's node_modules context, so a
// dynamic import() of these files breaks their own deps. The modules-with-toolbox set is
// asserted against disk below, so adding a module without adding it here fails rather than
// being silently skipped.
//
// The per-module `toolbox/api.js` files are deliberately absent: their register() mounts
// Express routes and registers ZERO tools (`grep -c 'server.tool(' modules/*/toolbox/api.js`
// -> 0 for all three), and importing express under vitest does not resolve — which is why the
// existing *-api.test.js files import only the pure handler functions too.
// email.js -> email-transport.js -> nodemailer, which does not resolve under vitest; mock the
// transport exactly as tests/email.test.js does. vi.mock is hoisted above the imports below.
vi.mock('../../modules/core/toolbox/email-transport.js', () => ({
  createTransporter: () => ({ sendMail: vi.fn() }),
}));

import { register as registerBitbucket } from '../../modules/bitbucket/toolbox/bitbucket.js';
import { register as registerBrowser } from '../../modules/core/toolbox/browser.js';
import { register as registerEmail } from '../../modules/core/toolbox/email.js';
import { register as registerSchedule } from '../../modules/core/toolbox/schedule.js';
import { register as registerGitHub } from '../../modules/github/toolbox/github.js';
import { register as registerJira } from '../../modules/jira/toolbox/jira.js';
import { register as registerOutlook } from '../../modules/outlook/toolbox/outlook.js';

// Keyed by the exact toolbox FILE each registrar comes from, so the coverage guard below can
// compare against what is on disk — a module-level comparison would let a new
// core/toolbox/new-tools.js escape execution without changing either side.
const REGISTRARS = {
  bitbucket: { 'bitbucket.js': registerBitbucket },
  core: { 'browser.js': registerBrowser, 'email.js': registerEmail, 'schedule.js': registerSchedule },
  github: { 'github.js': registerGitHub },
  jira: { 'jira.js': registerJira },
  outlook: { 'outlook.js': registerOutlook },
};

// Route-only files: they mount Express routes and register ZERO tools. Asserted below rather
// than trusted, and kept out of REGISTRARS because express does not resolve under vitest (which
// is why the existing *-api.test.js files import only pure handlers). Keyed by FULL path — a bare
// 'api.js' would auto-exempt every future module's api.js, including a re-exported registrar.
const ROUTE_ONLY_FILES = new Set([
  'bitbucket/api.js',
  'github/api.js',
  'jira/api.js',
  'outlook/api.js',
]);

// Support files: no register() export, so production never invokes them. Listed explicitly so an
// unlisted file fails the guard instead of being silently assumed harmless.
const SUPPORT_FILES = new Set([
  'bitbucket/api-handlers.js',
  'bitbucket/bitbucket-auth.js',
  'core/converters.js',
  'core/email-transport.js',
  'github/api-handlers.js',
  'github/env-guard.js',
  'github/github-auth.js',
  'jira/api-handlers.js',
  'jira/jira-proxy.js',
  'outlook/api-handlers.js',
  'outlook/credentials.js',
  'outlook/graph-auth.js',
]);

// The AUTHORITATIVE "every tool is declared" check for shipped modules.
//
// Python's module_validator scans toolbox JS textually, so it is best-effort: classifying a bare
// `/` (division vs regex) needs a real parser, and no token heuristic settles it. This test has no
// such limitation — it *executes* each module's register() with a recording server, so it sees
// exactly what the toolbox registers, including names computed at runtime (the browser
// passthrough) that no text scan could ever find.

const MODULES = fileURLToPath(new URL('../../modules', import.meta.url));

function declaredNames(moduleDir) {
  const manifest = JSON.parse(fs.readFileSync(path.join(moduleDir, 'module.json'), 'utf8'));
  return new Set((manifest.tools || []).map(t => t.name));
}

/** A permissive context: every module registers its tools before touching any of these. */
function stubContext(upstreamTools = []) {
  const auth = { isConfigured: () => true, bbFetch: vi.fn(), graphFetch: vi.fn() };
  return {
    log: vi.fn(),
    db: { query: vi.fn(), execute: vi.fn() },
    jobId: 1,
    agentViewId: 1,
    artifactsDir: '/tmp/artifacts',
    isToolEnabled: () => true,
    fileManager: { downloadAndConvert: vi.fn(), converterRegistry: { register: vi.fn() } },
    bitbucketAuthFactory: () => auth,
    graphAuthFactory: () => auth,
    playwright: {
      getClient: () => ({ callTool: vi.fn(), listTools: vi.fn() }),
      getTools: () => upstreamTools,
      getViewport: () => ({ width: 1366, height: 768 }),
      getState: () => ({ state: 'ready' }),
    },
    moduleConfigs: {
      core: { allowed_domains: 'example.com', smtp_host: 'smtp.example.com', email_whitelist: 'a@b.com' },
      jira: { jira_host: 'https://x.atlassian.net', jira_user: 'u@x.com', jira_token: 't' },
      bitbucket: { bitbucket_workspace: 'w', bitbucket_email: 'e@x.com', bitbucket_api_token: 't', repo_allowlist: 'r' },
      outlook: { outlook_tenant_id: 't', outlook_client_id: 'c', outlook_client_secret: 's', mailbox: 'm@x.com' },
    },
  };
}

async function registeredNames(moduleName, upstreamTools) {
  const names = [];
  const server = { tool: (name) => names.push(name) };
  for (const register of Object.values(REGISTRARS[moduleName])) {
    await register(server, stubContext(upstreamTools));
  }
  return names;
}

/** Every toolbox .js file on disk, per module. */
function toolboxFilesOnDisk() {
  const found = {};
  for (const entry of fs.readdirSync(MODULES, { withFileTypes: true })) {
    if (!entry.isDirectory() || entry.name.startsWith('_') || entry.name.startsWith('.')) continue;
    const toolboxDir = path.join(MODULES, entry.name, 'toolbox');
    if (!fs.existsSync(toolboxDir)) continue;
    found[entry.name] = fs.readdirSync(toolboxDir).filter(f => f.endsWith('.js')).sort();
  }
  return found;
}

/** Could production see a `register` export from this file?
 *
 * Production invokes any imported module where `typeof toolModule.register === 'function'`
 * (config-loader.js), so this deliberately over-matches: a direct binding of any kind, a named
 * re-export, `export *` (which can re-export a register), or a destructured export. Anything it
 * flags must be executed by this test or explicitly classified — over-matching costs a listing,
 * under-matching lets a production registrar escape. */
function mayExportRegister(src) {
  return /export\s+(async\s+function|function|const|let|var)\s+register\b/.test(src)
    || /export\s*\{[^}]*\bregister\b/.test(src)
    || /export\s*\*/.test(src)
    || /export\s+(const|let|var)\s*[{[]/.test(src);
}

describe('every registered tool is declared in module.json', () => {
  it('accounts for every toolbox file by full path', () => {
    // Exhaustive and path-keyed: every toolbox .js must be a listed registrar (executed below),
    // a listed route-only file (asserted tool-free below), or a listed support file. An unlisted
    // file fails here — a new core/toolbox/new-tools.js cannot escape, and no future module's
    // api.js is auto-exempted by basename.
    const unaccounted = [];
    for (const [mod, files] of Object.entries(toolboxFilesOnDisk())) {
      for (const file of files) {
        const full = `${mod}/${file}`;
        if (REGISTRARS[mod]?.[file] || ROUTE_ONLY_FILES.has(full) || SUPPORT_FILES.has(full)) continue;
        unaccounted.push(full);
      }
    }
    expect(unaccounted).toEqual([]);
  });

  it.each([...SUPPORT_FILES])('%s is support code and exports no register', (full) => {
    const [mod, file] = full.split('/');
    const src = fs.readFileSync(path.join(MODULES, mod, 'toolbox', file), 'utf8');
    expect(mayExportRegister(src)).toBe(false);
  });

  it.each([...ROUTE_ONLY_FILES])('%s mounts routes and registers no tools', (full) => {
    // Proven, not asserted by comment: importing these under vitest is impossible (express does
    // not resolve), so read the source and assert it makes no server.tool call and re-exports
    // nothing that could carry one.
    const [mod, file] = full.split('/');
    const src = fs.readFileSync(path.join(MODULES, mod, 'toolbox', file), 'utf8');
    expect(src).not.toMatch(/\bserver\s*\.\s*tool\s*\(/);
    expect(src).not.toMatch(/export\s*\*/);
  });

  it.each(Object.keys(REGISTRARS))('%s registers nothing it does not declare', async (name) => {
    const declared = declaredNames(path.join(MODULES, name));
    const registered = await registeredNames(name, []);
    const undeclared = [...new Set(registered)].filter(n => !declared.has(n));
    expect(undeclared).toEqual([]);
  });

  it('core declares exactly the tools the real Playwright MCP exposes in production', async () => {
    // The upstream list is NOT scraped from the package README: production starts the MCP child
    // with `--caps devtools` (playwright-client.js buildMcpArgs), which exposes a DIFFERENT set —
    // scraping the README declared 12 tools that never appear and missed 2 that do, leaving those
    // 2 invisible and denied. This fixture is the real `listTools()` result under those flags.
    //
    // To regenerate after a @playwright/mcp bump, from src/agento/toolbox:
    //   node -e "import('@modelcontextprotocol/sdk/client/stdio.js').then(async ({StdioClientTransport})=>{ \
    //     const {Client}=await import('@modelcontextprotocol/sdk/client/index.js'); \
    //     const t=new StdioClientTransport({command:'npx',args:['@playwright/mcp','--headless','--browser','chromium','--ignore-https-errors','--caps','devtools','--viewport-size','1366,768']}); \
    //     const c=new Client({name:'p',version:'1'},{capabilities:{}}); await c.connect(t); \
    //     console.log(JSON.stringify((await c.listTools()).tools.map(x=>x.name).sort())); await c.close(); })"
    const upstreamNames = [
      'browser_click', 'browser_close', 'browser_console_messages', 'browser_drag',
      'browser_evaluate', 'browser_file_upload', 'browser_fill_form', 'browser_handle_dialog',
      'browser_hover', 'browser_install', 'browser_navigate', 'browser_navigate_back',
      'browser_network_requests', 'browser_press_key', 'browser_resize', 'browser_run_code',
      'browser_select_option', 'browser_snapshot', 'browser_start_tracing', 'browser_start_video',
      'browser_stop_tracing', 'browser_stop_video', 'browser_tabs', 'browser_take_screenshot',
      'browser_type', 'browser_wait_for',
    ];
    const declared = declaredNames(path.join(MODULES, 'core'));
    const declaredBrowser = [...declared].filter(n => n.startsWith('browser_')).sort();

    // Every real tool is declared — otherwise the gate denies it and it is invisible in admin.
    expect(upstreamNames.filter(n => !declared.has(n))).toEqual([]);
    // And nothing is declared that production never exposes, which would be a phantom toggle.
    expect(declaredBrowser).toEqual([...upstreamNames].sort());

    // Feeding that inventory through the passthrough must register only declared names.
    const upstream = upstreamNames.map(name => ({
      name, description: name, inputSchema: { type: 'object', properties: {} },
    }));
    const registered = await registeredNames('core', upstream);
    expect([...new Set(registered)].filter(n => !declared.has(n))).toEqual([]);
    expect(registered).toContain('browser_click');
  });
});
