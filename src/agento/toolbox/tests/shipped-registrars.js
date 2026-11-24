import { vi } from 'vitest';
import { fileURLToPath } from 'node:url';

// Static imports: vite cannot resolve a computed specifier's node_modules context, so a
// dynamic import() of these files breaks their own deps. The modules-with-toolbox set is
// asserted against disk in tool-declaration.test.js, so adding a module without adding it here fails rather than
// being silently skipped.
//
// The per-module `toolbox/api.js` files are deliberately absent: their register() mounts
// Express routes and registers ZERO tools (`grep -c 'server.tool(' modules/*/toolbox/api.js`
// -> 0 for all three), and importing express under vitest does not resolve — which is why the
// existing *-api.test.js files import only the pure handler functions too.
import { register as registerBitbucket } from '../../modules/bitbucket/toolbox/bitbucket.js';
import { register as registerBrowser } from '../../modules/core/toolbox/browser.js';
import { register as registerEmail } from '../../modules/core/toolbox/email.js';
import { register as registerSchedule } from '../../modules/core/toolbox/schedule.js';
import { register as registerGitHub } from '../../modules/github/toolbox/github.js';
import { register as registerJira } from '../../modules/jira/toolbox/jira.js';
import { register as registerOutlook } from '../../modules/outlook/toolbox/outlook.js';
import { register as registerScheduler } from '../../modules/scheduler/toolbox/schedule-tools.js';
import { register as registerSubagent } from '../../modules/subagent/toolbox/agent.js';
import { register as registerVersionedArtifacts } from '../../modules/versioned_artifacts/toolbox/versioned-artifacts.js';

// Keyed by the exact toolbox FILE each registrar comes from, so the coverage guard below can
// compare against what is on disk — a module-level comparison would let a new
// core/toolbox/new-tools.js escape execution without changing either side.
export const REGISTRARS = {
  bitbucket: { 'bitbucket.js': registerBitbucket },
  core: { 'browser.js': registerBrowser, 'email.js': registerEmail, 'schedule.js': registerSchedule },
  github: { 'github.js': registerGitHub },
  jira: { 'jira.js': registerJira },
  outlook: { 'outlook.js': registerOutlook },
  scheduler: { 'schedule-tools.js': registerScheduler },
  subagent: { 'agent.js': registerSubagent },
  versioned_artifacts: { 'versioned-artifacts.js': registerVersionedArtifacts },
};

export const MODULES = fileURLToPath(new URL('../../modules', import.meta.url));

/** A permissive context: every module registers its tools before touching any of these. */
export function stubContext(upstreamTools = []) {
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
      // Every field Task 8's CONFIG fixture carries has to be here: createService
      // validates the whole configuration up front, so a missing limit throws,
      // register() returns having registered nothing, and the gate would report
      // this module's ten tools as undeclared — naming a manifest problem the
      // manifest does not have. storage_root is a path no toolbox can hold, and
      // no `app` is present, so the sweep never runs here.
      versioned_artifacts: {
        storage_root: '/nonexistent/vf-test-root',
        published_root: '/nonexistent/vf-test-published', allowed_artifacts: 'site',
        'limits/max_files': 2000, 'limits/max_file_size': 5242880,
        'limits/max_total_size': 104857600, 'limits/max_diff_bytes': 1048576,
        'limits/max_agent_artifacts': 50,
        'security/allow_symlinks': false,
      },
    },
  };
}

/** Every tool each registrar of `moduleName` registers: `{name, description, schema}`. */
export async function recordRegistrations(moduleName, upstreamTools = []) {
  const recorded = [];
  const server = { tool: (name, description, schema) => recorded.push({ name, description, schema }) };
  for (const register of Object.values(REGISTRARS[moduleName])) {
    await register(server, stubContext(upstreamTools));
  }
  return recorded;
}
