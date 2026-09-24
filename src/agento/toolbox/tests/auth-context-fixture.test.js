import { describe, it, expect } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import {
  AuthConfigError, computeAuthTtls, deriveAuthContext, ENDPOINT_TRANSPORT, LEGACY_REST_SUBJECT, TTL_CEILINGS,
} from '../auth-context.js';

const here = path.dirname(fileURLToPath(import.meta.url));
// Repo-root fixture, read by the Python issuer's test too — one table, two implementations.
const fixtureFile = path.join(here, '..', '..', '..', '..', 'tests', 'fixtures', 'auth_context_v1.json');
const fixture = JSON.parse(fs.readFileSync(fixtureFile, 'utf8'));

describe('auth context v1 shared fixture (Node verifier side)', () => {
  it.each(fixture.cases.map(c => [c.why, c]))('%s', (_why, c) => {
    const got = deriveAuthContext({
      row: c.row,
      agentViewWorkspaceId: c.agent_view_workspace_id,
      source: c.source,
      endpoint: c.endpoint,
      ttlCaps: fixture.ttl_caps,
    });
    expect(got).toEqual(c.expected);
  });

  it('shares the constants the Python side asserts', () => {
    expect(TTL_CEILINGS).toEqual(fixture.ttl_ceilings);
    expect(LEGACY_REST_SUBJECT).toBe(fixture.legacy_rest_subject);
    expect(ENDPOINT_TRANSPORT).toEqual(fixture.endpoint_transport);
  });
});

describe('core/auth/* TTL resolution shared fixture (Node side)', () => {
  it.each(fixture.ttl_resolution.map(c => [c.why, c]))('%s', (_why, c) => {
    const run = () => computeAuthTtls({
      env: c.env,
      defaultOverrides: c.default_overrides,
      workspaceOverrides: c.workspace_overrides,
      configDefaults: c.config_defaults,
    });
    if (c.expected === 'error') expect(run).toThrow(AuthConfigError);
    else expect(run()).toEqual(c.expected);
  });
});
