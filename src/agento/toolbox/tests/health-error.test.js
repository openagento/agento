import { describe, it, expect } from 'vitest';
import { HEALTH_ERROR_CATEGORIES, runHealthchecks, sanitizeHealthError } from '../health-run.js';

// Class guard: every healthcheck result that reaches a caller must carry a stable category,
// never driver text. Written against runHealthchecks — the single funnel — so an adapter
// added later inherits the guarantee without an opt-in.
describe('healthcheck error sanitization', () => {
  const SECRETS = [
    "Access denied for user 'agento'@'10.0.0.4' (using password: YES)",
    'connect ECONNREFUSED 10.0.0.9:3306',
    'AADSTS7000215: Invalid client secret provided: abc123',
    'getaddrinfo ENOTFOUND smtp.internal.corp',
    'HTTP 503 from https://jira.internal/rest/api/3/myself',
  ];

  it('never returns driver text for a failing check', async () => {
    const healthchecks = SECRETS.map(msg => async () => [
      { tool: 'x', status: 'fail', ms: 1, error: msg },
    ]);
    const checks = await runHealthchecks(healthchecks);
    expect(checks).toHaveLength(SECRETS.length);
    for (const check of checks) {
      expect(HEALTH_ERROR_CATEGORIES).toContain(check.error);
    }
    const rendered = JSON.stringify(checks);
    for (const fragment of ['password', 'abc123', '10.0.0.9', 'smtp.internal.corp', 'jira.internal']) {
      expect(rendered).not.toContain(fragment);
    }
  });

  it('sanitizes a rejected healthcheck too', async () => {
    const checks = await runHealthchecks([async () => { throw new Error('connect ECONNREFUSED 10.0.0.9:3306'); }]);
    expect(checks[0]).toEqual({ tool: 'unknown', status: 'fail', error: 'unreachable' });
  });

  it('leaves a passing check untouched', async () => {
    const checks = await runHealthchecks([async () => [{ tool: 'x', status: 'ok', ms: 3 }]]);
    expect(checks[0]).toEqual({ tool: 'x', status: 'ok', ms: 3 });
  });

  // The producer contract (docs/tools/creating-an-adapter.md) says an adapter picks the
  // category itself and the funnel is only a backstop. That composes ONLY if a second pass is
  // a no-op: 'unreachable' matches none of the patterns and used to degrade to 'failed'.
  it('is idempotent over every category, so the funnel cannot downgrade an adapter', () => {
    for (const category of HEALTH_ERROR_CATEGORIES) {
      expect(sanitizeHealthError(category)).toBe(category);
      expect(sanitizeHealthError(sanitizeHealthError(category))).toBe(category);
    }
  });

  // Collisions: an auth WORD inside a hostname or a service name must not outrank the
  // transport evidence sitting next to it. An operator sent to 'auth failed' checks the
  // credential; the actual fault is that the host never answered.
  it('prefers transport evidence over auth vocabulary', () => {
    expect(sanitizeHealthError('getaddrinfo ENOTFOUND login.internal')).toBe('unreachable');
    expect(sanitizeHealthError('connect ECONNREFUSED credential-service.internal')).toBe('unreachable');
    expect(sanitizeHealthError('HTTP 503 authentication service unavailable')).toBe('unreachable');
    expect(sanitizeHealthError('ETIMEDOUT contacting token.internal')).toBe('timeout');
  });

  it('still reports a real auth failure', () => {
    expect(sanitizeHealthError('HTTP 401 Unauthorized')).toBe('auth failed');
    expect(sanitizeHealthError("Access denied for user 'agento'@'10.0.0.4'")).toBe('auth failed');
    expect(sanitizeHealthError('AADSTS7000215: Invalid client secret provided')).toBe('auth failed');
  });

  it('maps raw text to a stable category', () => {
    expect(sanitizeHealthError('operation timed out')).toBe('timeout');
    expect(sanitizeHealthError('HTTP 401 Unauthorized')).toBe('auth failed');
    expect(sanitizeHealthError('connect ECONNREFUSED')).toBe('unreachable');
    expect(sanitizeHealthError('mysql host not configured')).toBe('misconfigured');
    expect(sanitizeHealthError('something else entirely')).toBe('failed');
  });
});

// Structural guard for the producer contract itself: a `status: 'fail'` result whose `error`
// is `err.message` hands the funnel a raw driver string. Restricted to the files that
// legitimately contain a healthcheck producer, so it cannot fire on prose or a changelog.
describe('healthcheck producers return a category, not driver text', () => {
  const PRODUCERS = [
    '../adapters/mysql.js',
    '../adapters/mssql.js',
    '../adapters/opensearch.js',
    '../../modules/core/toolbox/browser.js',
    '../../modules/core/toolbox/email.js',
    '../../modules/core/toolbox/schedule.js',
    '../../modules/jira/toolbox/jira.js',
  ];

  it('no producer puts err.message in a fail result', async () => {
    const { readFile } = await import('node:fs/promises');
    const { fileURLToPath } = await import('node:url');
    const offenders = [];
    for (const rel of PRODUCERS) {
      const path = fileURLToPath(new URL(rel, import.meta.url));
      const src = await readFile(path, 'utf8');
      for (const line of src.split('\n')) {
        if (line.includes("status: 'fail'") && /error:\s*err\??\.message/.test(line)) {
          offenders.push(`${rel}: ${line.trim()}`);
        }
      }
    }
    expect(offenders).toEqual([]);
  });
});
