import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import express from 'express';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { createRateLimits } from '../rate-limit.js';
import { extractToken } from '../capability.js';

const VALID = new Set(['good-a', 'good-b']);
let server;
let baseUrl;

beforeAll(async () => {
  const app = express();
  app.use(...createRateLimits({ authFailuresPerAddress: 3, requestsPerCapability: 5 }));
  // A stand-in for requireCapability: 401 unless the token is known.
  app.get('/guarded', (req, res) => (VALID.has(extractToken(req)) ? res.json({ ok: true }) : res.status(401).end()));
  await new Promise((resolve) => { server = app.listen(0, '127.0.0.1', resolve); });
  baseUrl = `http://127.0.0.1:${server.address().port}`;
});

afterAll(() => new Promise((resolve) => server.close(resolve)));

const call = (token) => fetch(`${baseUrl}/guarded`, token ? { headers: { authorization: `Bearer ${token}` } } : {});

describe('toolbox rate limits', () => {
  it('limits each capability separately and does not count its successes as auth failures', async () => {
    for (let i = 0; i < 5; i++) expect((await call('good-a')).status).toBe(200);
    expect((await call('good-a')).status).toBe(429);
    // Five successes from this address did not use up the auth-failure budget of 3.
    expect((await call('good-b')).status).toBe(200);
  });

  it('bounds a random-token flood per address, whatever the tokens', async () => {
    for (let i = 0; i < 3; i++) expect((await call(`random-${i}`)).status).toBe(401);
    expect((await call('random-next')).status).toBe(429);
    expect((await call()).status).toBe(429);
  });
});

describe('server.js', () => {
  it('mounts the rate limits before its first route', () => {
    // A text check on this one file (TST-2): server.js starts a listener on import, so its route
    // table cannot be loaded in a test.
    const src = readFileSync(fileURLToPath(new URL('../server.js', import.meta.url)), 'utf8');
    const limits = src.indexOf('app.use(...createRateLimits())');
    const firstRoute = src.search(/\bapp\.(get|post|put|patch|delete|all|use)\(\s*['"]/);
    expect(limits).toBeGreaterThan(-1);
    expect(firstRoute).toBeGreaterThan(-1);
    expect(limits).toBeLessThan(firstRoute);
  });
});
