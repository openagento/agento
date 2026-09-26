import fs from 'node:fs';
import path from 'node:path';
import { describe, it, expect } from 'vitest';

import { authSources, checkSession, GRANTS_SQL } from '../../modules/web/toolbox/auth-sources.js';

const FIXTURE = JSON.parse(fs.readFileSync(
  path.join(import.meta.dirname, '../../../../tests/fixtures/role_grant_v1.json'), 'utf8'));

// Symbolic scopes -> ids, the same shape the Python test builds in MySQL.
const IDS = { ws1: 1, ws2: 2, av1: 11, av2: 22 };
const VIEW_WORKSPACE = { 11: 1, 22: 2 };
const GRANTS = FIXTURE.grants.map(g => ({
  ...g, workspace_id: IDS[g.workspace] ?? null, agent_view_id: IDS[g.agent_view] ?? null,
}));
const LIVE = { id: 'abc', user_id: 7, role: 'user', created_at: 1000, expires_at: 2000 };

// Evaluates the checker's own SQL over in-memory rows; the grant WHERE is applied exactly
// as the SQL states it, NULL never equal to anything.
function fakeQuery({ session = LIVE } = {}) {
  const eq = (a, b) => a !== null && b !== null && a === b;
  return async (sql, params) => {
    if (sql.includes('FROM agent_view')) {
      const ws = VIEW_WORKSPACE[params[0]];
      return [ws ? [{ workspace_id: ws }] : []];
    }
    if (sql.includes('FROM session')) return [session && params[0] === session.id ? [session] : []];
    if (sql === GRANTS_SQL) {
      const [role, kind, view, ws] = params;
      const names = GRANTS.filter(g => g.role === role && g.grant_kind === kind && (
        (eq(g.agent_view_id, view) && g.workspace_id === null) ||
        (g.agent_view_id === null && eq(g.workspace_id, ws)))).map(g => g.name);
      return [[...new Set(names)].sort().map(name => ({ name }))];
    }
    throw new Error(`unexpected SQL: ${sql}`);
  };
}

const opts = (over = {}) => ({ capability_kind: 'user_session', workspace_id: 1, agent_view_id: 11, query: fakeQuery(), ...over });

describe('web session auth source', () => {
  it('exports the session kind', () => {
    expect(authSources).toEqual([['session', checkSession]]);
  });

  it('returns the row scope and the grants for it', async () => {
    expect(await checkSession('abc', opts())).toEqual({
      kind: 'session', id: 'abc', user_id: '7', workspace_id: 1, agent_view_id: 11,
      permitted_tools: ['tool_av1', 'tool_ws1'], created_at: 1000, expires_at: 2000,
    });
  });

  it('refuses a session the query no longer finds (revoked, expired, inactive user)', async () => {
    expect(await checkSession('abc', opts({ query: fakeQuery({ session: null }) }))).toBeNull();
    expect(await checkSession('other', opts())).toBeNull();
  });

  it('refuses another capability kind', async () => {
    expect(await checkSession('abc', opts({ capability_kind: 'miniapp' }))).toBeNull();
  });

  it('refuses a scope with no grant', async () => {
    expect(await checkSession('abc', opts({ workspace_id: 2, agent_view_id: null }))).toBeNull();
  });

  it('refuses a view that is not in the workspace, and a bad workspace id', async () => {
    expect(await checkSession('abc', opts({ workspace_id: 2, agent_view_id: 11 }))).toBeNull();
    expect(await checkSession('abc', opts({ workspace_id: null }))).toBeNull();
    expect(await checkSession('abc', opts({ workspace_id: '1' }))).toBeNull();
  });

  for (const c of FIXTURE.cases.filter(x => x.grant_kind === 'tool')) {
    it(`matches the shared fixture: ${c.role} ${c.workspace}/${c.agent_view}`, async () => {
      const session = { ...LIVE, role: c.role };
      const got = await checkSession('abc', opts({
        workspace_id: IDS[c.workspace], agent_view_id: IDS[c.agent_view] ?? null, query: fakeQuery({ session }),
      }));
      if (c.expected.length === 0) expect(got).toBeNull();
      else expect(got.permitted_tools).toEqual(c.expected);
    });
  }
});
