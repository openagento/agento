import { describe, it, expect, vi } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createVerifier, createRequireCapability, createSourceLookup } from '../capability.js';
import { ENDPOINTS } from '../auth-context.js';
import { capabilityRow } from './capability-rows.js';

const here = path.dirname(fileURLToPath(import.meta.url));
const T0 = Math.floor(Date.now() / 1000);

// Live sources that agree with every claim, so a refusal below can only come from the
// endpoint or transport rule — never from a missing checker.
const userRows = {
  user_session: {
    ...capabilityRow('mcp_job'), kind: 'user_session', actor: 'user', subject_id: 'u-9',
    agent_view_id: null, agent_view_workspace_id: null, job_id: null,
    source_kind: 'session', source_id: 's-1', created_at: T0, expires_at: T0 + 30,
  },
  miniapp: {
    ...capabilityRow('mcp_job'), kind: 'miniapp', actor: 'user', subject_id: 'u-9',
    agent_view_id: null, agent_view_workspace_id: null, job_id: null,
    app_artifact_code: 'crm', app_version_id: 2, app_launch_id: 'l-1', tool_ceiling: ['email_send'],
    source_kind: 'launch', source_id: 'l-1', created_at: T0, expires_at: T0 + 30,
  },
};
const base = { user_id: 'u-9', workspace_id: 3, agent_view_id: null, permitted_tools: ['email_send'],
  created_at: T0 - 10, expires_at: T0 + 600 };
const sources = createSourceLookup([
  ['session', async () => ({ ...base, kind: 'session', id: 's-1' })],
  ['launch', async () => ({ ...base, kind: 'launch', id: 'l-1', launch_id: 'l-1',
    artifact_code: 'crm', version_id: 2 })],
]);
const resolveTtls = async () => ({ session_max_ttl: 43200, launch_max_ttl: 3600, capability_ttl: 30 });

function guardFor(row, endpoint, log = () => {}) {
  const verify = createVerifier(async () => [[row]], { sourceCheckers: sources, resolveTtls });
  return createRequireCapability(verify, { endpoint }, log);
}

async function present(guard, { query = {}, header = 'Bearer tok' } = {}) {
  const res = { code: null };
  res.status = (c) => { res.code = c; return res; };
  res.json = () => res;
  const next = vi.fn();
  await guard({ headers: header ? { authorization: header } : {}, query }, res, next);
  return next.mock.calls.length ? 200 : res.code;
}

describe('the routes name the endpoints the rules are written for', () => {
  it('maps each guarded route to its endpoint', () => {
    const src = fs.readFileSync(path.join(here, '..', 'server.js'), 'utf8');
    const routes = Object.fromEntries(
      [...src.matchAll(/app\.(?:get|post|all|use)\(\s*'([^']+)',\s*requireCapability\(\{\s*endpoint:\s*'([a-z_]+)'/g)]
        .map(m => [m[1], m[2]]),
    );
    expect(routes).toEqual({
      '/sse': 'sse', '/messages': 'messages', '/mcp': 'mcp', '/config-test': 'config_test', '/api': 'api',
    });
    expect(src).toMatch(/const healthGuard = requireCapability\(\{ endpoint: 'health' \}/);
    expect(src).toMatch(/installInvokeRoute\(app, \{\s*guard: requireCapability\(\{ endpoint: 'invoke' \}/);
  });
});

describe('new profiles are accepted only at invoke', () => {
  for (const kind of Object.keys(userRows)) {
    for (const endpoint of ['mcp', 'sse', 'messages', 'api', 'config_test']) {
      it(`${kind} at ${endpoint} → 403`, async () => {
        const row = { ...userRows[kind], allowed_transports: ['sse', 'http'] };
        expect(await present(guardFor(row, endpoint))).toBe(403);
      });
    }
    it(`${kind} at invoke → accepted (control)`, async () => {
      expect(await present(guardFor(userRows[kind], 'invoke'))).toBe(200);
    });
  }
});

describe('transports', () => {
  it('an ["http"] token at /sse and /messages → 403, at /mcp → accepted', async () => {
    const row = capabilityRow('mcp_job', { allowed_transports: ['http'] });
    expect(await present(guardFor(row, 'sse'))).toBe(403);
    expect(await present(guardFor(row, 'messages'))).toBe(403);
    expect(await present(guardFor(row, 'mcp'))).toBe(200);
  });

  it('missing allowed_transports → 403 at every endpoint, for every kind', async () => {
    const rows = [
      capabilityRow('mcp_job'), capabilityRow('mcp_interactive'), capabilityRow('internal_rest'),
      ...Object.values(userRows),
    ];
    for (const row of rows) {
      for (const endpoint of ENDPOINTS) {
        expect(await present(guardFor({ ...row, allowed_transports: null }, endpoint))).toBe(403);
      }
    }
  });

  it('a query-string token at invoke → 401, even for a row valid there', async () => {
    const guard = guardFor(userRows.user_session, 'invoke');
    expect(await present(guard, { query: { cap: 'tok' } })).toBe(401);
    expect(await present(guard, { query: { cap: 'tok' }, header: null })).toBe(401);
  });
});

describe('cap redaction', () => {
  it('no guard error path logs the token from ?cap=', async () => {
    const lines = [];
    const log = (...a) => lines.push(a.map(String).join(' '));
    const rows = [capabilityRow('mcp_job', { allowed_transports: ['http'] }), userRows.user_session];
    for (const row of rows) {
      for (const endpoint of ENDPOINTS) {
        await present(guardFor(row, endpoint, log), { query: { cap: 'secret-cap-value' }, header: null });
      }
    }
    const failing = createRequireCapability(async () => { throw new Error('db down'); }, { endpoint: 'mcp' }, log);
    await present(failing, { query: { cap: 'secret-cap-value' }, header: null });
    expect(lines.length).toBeGreaterThan(0);
    expect(lines.join('\n')).not.toContain('secret-cap-value');
  });
});
